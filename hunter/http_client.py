"""Shared HTTP client infrastructure — typed errors, Retry-After parsing,
consistent User-Agent, and context-managed paced HttpSession.

Layer 1 — imports stdlib, requests, hunter.__version__.
"""
from __future__ import annotations

import email.utils
import math
import os
import threading
import time
import weakref
from datetime import datetime, timezone
from typing import Any

import requests

from hunter import __version__


class HttpError(RuntimeError):
    """HTTP transport or status error with optional status & Retry-After."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class RateLimitError(HttpError):
    """HTTP 429 rate-limit error."""

    def __init__(
        self,
        message: str = "rate limited",
        *,
        status: int | None = 429,
        retry_after: float | None = None,
    ):
        super().__init__(message, status=status, retry_after=retry_after)


def parse_retry_after(
    header: str | None,
    default: float = 5.0,
    cap: float = 60.0,
) -> float:
    """Parse an HTTP ``Retry-After`` header (numeric seconds or HTTP-date).

    Clamps negative values to ``>= 0.0`` and caps at *cap*.
    """
    cap = max(0.0, float(cap))
    fallback = min(max(0.0, float(default)), cap)
    if header is None:
        return fallback
    raw = str(header).strip()
    if not raw:
        return fallback
    try:
        secs = float(raw)
        if math.isfinite(secs):
            return min(max(0.0, secs), cap)
        return fallback
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return fallback
    if dt is None:
        return fallback
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delay = (dt - datetime.now(timezone.utc)).total_seconds()
    return min(max(0.0, delay), cap)


def default_user_agent(cfg: Any = None, component: str = "hunter") -> str:
    """Build a standard User-Agent using ``cfg.app_name`` (or ``HUNTER_APP_NAME``)
    and ``hunter.__version__`` plus optional operator contact from
    ``cfg.user_agent_contact`` (no hardcoded PII)."""
    app_cfg = (getattr(cfg, "app_name", "") or "").strip() if cfg is not None else ""
    app = app_cfg or (os.environ.get("HUNTER_APP_NAME") or "").strip() or "Hunter"
    comp = (component or "hunter").strip()
    base = f"{app}-{comp}/{__version__}"
    contact = ""
    if cfg is not None:
        contact = (getattr(cfg, "user_agent_contact", "") or "").strip()
    return f"{base} ({contact})" if contact else base


class RateLimiter:
    """Thread-safe monotonic slot scheduler for polite public APIs (MB, LB).

    Reserves the next slot under lock and sleeps outside the lock so waiting
    threads queue cleanly without blocking slot reservation.
    """

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def acquire(self, min_interval: float | None = None) -> None:
        step = self._min_interval if min_interval is None else max(0.0, float(min_interval))
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + step
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def push_back(self, delay: float) -> None:
        """Advance the next allowed slot by at least *delay* seconds (e.g. Retry-After)."""
        wait = max(0.0, float(delay))
        if wait <= 0:
            return
        with self._lock:
            self._next_slot = max(self._next_slot, time.monotonic() + wait)

    def reset(self) -> None:
        with self._lock:
            self._next_slot = 0.0


class PoliteClient:
    """Shared rate-limited, per-thread ``HttpSession`` manager for public APIs
    (MusicBrainz, ListenBrainz, Cover Art Archive).

    Uses ``weakref.finalize`` on each thread so short-lived worker threads
    automatically close their underlying ``requests.Session`` sockets on exit.
    """

    def __init__(
        self,
        *,
        timeout: float = 12.0,
        min_interval: float = 1.0,
        component: str = "hunter",
    ) -> None:
        self._timeout = float(timeout)
        self._component = component
        self.limiter = RateLimiter(min_interval)
        self._local = threading.local()

    def user_agent(self, cfg: Any = None) -> str:
        eff = cfg
        if eff is None:
            try:
                from hunter.config import HunterConfig
                eff = HunterConfig.from_env()
            except Exception:
                eff = None
        return default_user_agent(eff, component=self._component)

    def session(self, cfg: Any = None) -> HttpSession:
        ua = self.user_agent(cfg)
        sess: HttpSession | None = getattr(self._local, "http", None)
        if sess is None:
            sess = HttpSession(timeout=self._timeout, user_agent=ua)
            self._local.http = sess
            weakref.finalize(threading.current_thread(), sess.close)
        else:
            sess.headers["User-Agent"] = ua
        return sess


class HttpSession:
    """Context-managed ``requests.Session`` wrapper with strict timeouts,
    safe JSON decoding, Retry-After handling, and monotonic pacing."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        min_interval: float = 0.0,
        max_retries: int = 0,
    ):
        self._session = requests.Session()
        self._timeout = float(timeout)
        self._min_interval = max(0.0, float(min_interval))
        self._max_retries = max(0, int(max_retries))
        self._last_request_mono: float | None = None
        self._session.headers["User-Agent"] = user_agent or default_user_agent()
        if headers:
            self._session.headers.update(headers)
        if proxy:
            proxy_str = proxy.strip()
            if proxy_str:
                self._session.proxies.update({"http": proxy_str, "https": proxy_str})

    @property
    def headers(self):
        return self._session.headers

    @property
    def proxies(self):
        return self._session.proxies

    def __enter__(self) -> "HttpSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def clear_cookies(self) -> None:
        self._session.cookies.clear()

    def _pace(self) -> None:
        if self._min_interval > 0 and self._last_request_mono is not None:
            wait = self._min_interval - (time.monotonic() - self._last_request_mono)
            if wait > 0:
                time.sleep(wait)
        self._last_request_mono = time.monotonic()

    def request(
        self,
        method: str,
        url: str,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        retry_statuses: frozenset[int] = frozenset({429, 502, 503, 504}),
        retry_cap: float = 60.0,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        eff_timeout = self._timeout if timeout is None else float(timeout)
        attempts = (self._max_retries if retries is None else max(0, int(retries))) + 1

        for attempt in range(attempts):
            self._pace()
            try:
                resp = self._session.request(
                    method, url, timeout=eff_timeout, **kwargs
                )
            except requests.RequestException as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2.0 ** min(attempt, 30), retry_cap))
                    continue
                raise HttpError(f"{method.upper()} {url}: {exc}") from exc

            if resp.status_code == 429:
                ra = parse_retry_after(
                    resp.headers.get("Retry-After"), default=5.0, cap=retry_cap
                )
                if 429 in retry_statuses and attempt + 1 < attempts:
                    resp.close()
                    time.sleep(ra)
                    continue
                if raise_for_status:
                    resp.close()
                    raise RateLimitError(
                        f"HTTP 429 on {url}", status=429, retry_after=ra
                    )
                return resp

            if resp.status_code in retry_statuses and attempt + 1 < attempts:
                ra = parse_retry_after(
                    resp.headers.get("Retry-After"),
                    default=min(2.0 ** min(attempt, 30), retry_cap),
                    cap=retry_cap,
                )
                resp.close()
                time.sleep(ra)
                continue

            if raise_for_status and resp.status_code >= 400:
                ra = (
                    parse_retry_after(
                        resp.headers.get("Retry-After"), default=0.0, cap=retry_cap
                    )
                    if "Retry-After" in resp.headers
                    else None
                )
                status = resp.status_code
                resp.close()
                raise HttpError(
                    f"HTTP {status} on {url}",
                    status=status,
                    retry_after=ra,
                )
            return resp

        raise HttpError(f"{method.upper()} {url}: exhausted attempts")

    def request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = self.request(method, url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(
                f"invalid JSON from {url}: {exc}", status=resp.status_code
            ) from exc

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.request_json("GET", url, **kwargs)

    def post_json(self, url: str, **kwargs: Any) -> Any:
        return self.request_json("POST", url, **kwargs)


__all__ = [
    "HttpError",
    "HttpSession",
    "PoliteClient",
    "RateLimitError",
    "RateLimiter",
    "default_user_agent",
    "parse_retry_after",
]

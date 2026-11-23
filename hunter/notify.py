"""Rate-limited webhook push — ntfy / Discord / Gotify.

Uses an flock-guarded atomic rate-limit state file in /state so concurrent
PID 1 and pass subprocesses never race, with separate ``"routine"`` vs
``"alert"`` buckets and ``force=True`` so routine pass summaries never
silence critical alerts.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

from hunter import lock
from hunter.domain import atomic_write_json
from hunter.http_client import HttpError, HttpSession

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 10


def _detect_kind(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower().rstrip("/")
    query = parsed.query.lower()
    if host in ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com") and "/api/webhooks/" in path:
        return "discord"
    if path.endswith("/message") or "token=" in query:
        return "gotify"
    return "ntfy"


class Notifier:
    def __init__(self, webhook_url: str, min_interval: int, state_dir: Path):
        self.url = (webhook_url or "").strip()
        self.min_interval = max(0, int(min_interval))
        self._state_dir = Path(state_dir)
        self._stamp = self._state_dir / "last_notify.txt"
        self._lock_path = self._state_dir / "notify.lock"

    def _locked(self):
        return lock.hold(self._lock_path, blocking=True)

    def _read_buckets(self) -> dict[str, float]:
        try:
            raw = self._stamp.read_text(encoding="utf-8").strip()
        except OSError:
            return {}
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            try:
                return {"routine": float(raw)}
            except ValueError:
                return {}
        if isinstance(parsed, (int, float)):
            return {"routine": float(parsed)}
        if isinstance(parsed, dict):
            out: dict[str, float] = {}
            for k, v in parsed.items():
                if isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[k] = float(v)
            return out
        return {}

    def _write_bucket(self, bucket: str, ts: float) -> None:
        buckets = self._read_buckets()
        buckets[bucket] = ts
        atomic_write_json(self._stamp, buckets, indent=None)

    def _post(self, message: str, *, title: str, tags: list[str] | None, priority: int) -> None:
        kind = _detect_kind(self.url)
        with HttpSession(timeout=HTTP_TIMEOUT) as session:
            if kind == "discord":
                session.post(
                    self.url,
                    json={"content": message},
                )
            elif kind == "gotify":
                session.post(
                    self.url,
                    json={"title": title, "message": message, "priority": priority},
                )
            else:
                session.post(
                    self.url,
                    data=message.encode("utf-8"),
                    headers={
                        "Title": title,
                        "Tags": ",".join(tags or ["musical_note"]),
                    },
                )

    def send(
        self,
        message: str,
        *,
        force: bool = False,
        tags: list[str] | None = None,
        bucket: str = "routine",
        title: str = "hunter",
        priority: int = 5,
    ) -> bool:
        if not self.url:
            return False
        bucket_key = bucket or "routine"

        if force:
            try:
                self._post(message, title=title, tags=tags, priority=priority)
            except HttpError as exc:
                log.warning("notification failed: %s", exc)
                return False
            try:
                with self._locked():
                    self._write_bucket(bucket_key, time.time())
            except OSError as exc:
                log.debug("notification timestamp write failed: %s", exc)
            return True

        try:
            with self._locked():
                buckets = self._read_buckets()
                last = buckets.get(bucket_key)
                if last is not None and (time.time() - last) < self.min_interval:
                    log.debug("notification suppressed (rate limit, bucket=%s): %s", bucket_key, message)
                    return False
                try:
                    self._post(message, title=title, tags=tags, priority=priority)
                except HttpError as exc:
                    log.warning("notification failed: %s", exc)
                    return False
                try:
                    self._write_bucket(bucket_key, time.time())
                except OSError as exc:
                    log.debug("notification timestamp write failed: %s", exc)
                return True
        except OSError as exc:
            log.warning("notification lock unavailable, sending unthrottled: %s", exc)
            try:
                self._post(message, title=title, tags=tags, priority=priority)
                return True
            except HttpError as post_exc:
                log.warning("notification failed: %s", post_exc)
                return False


__all__ = ["HTTP_TIMEOUT", "Notifier"]

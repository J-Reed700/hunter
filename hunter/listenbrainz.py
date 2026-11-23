"""ListenBrainz client — popularity data for essential-albums scoring.

Free, no-auth-needed public endpoints. Used to rank an artist's release-
groups by how much the world actually listens to them, so hunter can tell
"you own 3 of Bowie's 9 essentials" and one-tap fill only the canon gaps.
"""
from __future__ import annotations

import logging
import math
import threading
import time

from hunter.config import HunterConfig
from hunter.http_client import (
    HttpError,
    HttpSession,
    default_user_agent,
    parse_retry_after,
)

log = logging.getLogger("hunter.listenbrainz")

LB = "https://api.listenbrainz.org/1"
_TIMEOUT = 12
_MIN_INTERVAL = 1.05
_rate_lock = threading.Lock()
_next_slot = 0.0
_thread_local = threading.local()


def _user_agent(cfg: HunterConfig | None = None) -> str:
    eff = cfg
    if eff is None:
        try:
            eff = HunterConfig.from_env()
        except Exception:                           # noqa: BLE001
            eff = None
    return default_user_agent(eff)


def _session(cfg: HunterConfig | None = None) -> HttpSession:
    """Return a per-thread HttpSession with dynamically computed User-Agent."""
    ua = _user_agent(cfg)
    sess = getattr(_thread_local, "http", None)
    if sess is None:
        sess = HttpSession(timeout=_TIMEOUT, user_agent=ua)
        _thread_local.http = sess
    else:
        sess.headers["User-Agent"] = ua
    return sess


def _reserve_slot(min_interval: float = _MIN_INTERVAL) -> None:
    """Reserve the next rate-limit slot under lock, then sleep outside lock."""
    global _next_slot
    with _rate_lock:
        now = time.monotonic()
        slot = max(now, _next_slot)
        _next_slot = slot + min_interval
    delay = slot - now
    if delay > 0:
        time.sleep(delay)


def release_group_listens_checked(
    rgids: list[str],
    *,
    cfg: HunterConfig | None = None,
) -> tuple[dict[str, int], bool]:
    """Return ``({rgid: total_listen_count}, complete)`` for a batch of
    release-group MBIDs. ``complete`` is False if any batch failed or returned
    malformed data so callers don't poison caches with partial results."""
    global _next_slot
    clean = list(dict.fromkeys(r.strip() for r in rgids if isinstance(r, str) and r.strip()))
    if not clean:
        return {}, True

    http = _session(cfg)
    out: dict[str, int] = {}
    complete = True
    for i in range(0, len(clean), 50):        # LB caps the batch at 50
        batch = clean[i:i + 50]
        batch_ok = False
        for attempt in range(2):
            _reserve_slot()
            try:
                resp = http.post(
                    f"{LB}/popularity/release-group",
                    json={"release_group_mbids": batch},
                    timeout=_TIMEOUT,
                    raise_for_status=False,
                )
            except HttpError as exc:
                log.warning("LB rg-popularity failed: %s", exc)
                break

            if resp.status_code == 429:
                ra = parse_retry_after(resp.headers.get("Retry-After"), default=2.0, cap=30.0)
                with _rate_lock:
                    _next_slot = max(_next_slot, time.monotonic() + ra)
                if attempt == 0:
                    log.warning("LB rate-limited (429) — waiting %.1fs", ra)
                    continue
                log.warning("LB rate-limited (429) on retry — marking incomplete")
                break

            if resp.status_code != 200:
                log.warning("LB rg-popularity HTTP %d: %s",
                            resp.status_code, resp.text[:120])
                break

            try:
                rows = resp.json()
            except ValueError as exc:
                log.warning("LB rg-popularity invalid JSON: %s", exc)
                break

            if not isinstance(rows, list):
                log.warning("LB rg-popularity expected list, got %s", type(rows).__name__)
                break

            for x in rows:
                if not isinstance(x, dict):
                    continue
                rgid = x.get("release_group_mbid")
                if not isinstance(rgid, str) or not rgid:
                    continue
                try:
                    cnt = int(x.get("total_listen_count") or 0)
                except (TypeError, ValueError):
                    cnt = 0
                out[rgid] = cnt
            batch_ok = True
            break

        if not batch_ok:
            complete = False
    return out, complete


def release_group_listens(rgids: list[str]) -> dict[str, int]:
    """{rgid: total_listen_count} for a batch of release-groups."""
    counts, _complete = release_group_listens_checked(rgids)
    return counts


def canon_scores_checked(artist_mbid: str, rgs: list[dict]) -> tuple[list[dict], bool]:
    """Return ``(scored, complete)`` normalized essentiality per release-group."""
    rgids = [r.get("rgid", "") for r in rgs if isinstance(r, dict)]
    listens, complete = release_group_listens_checked(rgids)
    scored = []
    for rg in rgs:
        if not isinstance(rg, dict):
            continue
        rgid = rg.get("rgid")
        if not rgid:
            continue
        n = listens.get(rgid, 0)
        if n <= 0:
            continue
        scored.append({
            "rgid": rgid,
            "name": rg.get("album", ""),
            "year": rg.get("year", 0),
            "listens": n,
        })
    if not scored:
        return [], complete
    lt = math.log1p(max(s["listens"] for s in scored))
    for s in scored:
        s["score"] = round(math.log1p(s["listens"]) / lt, 3) if lt else 0.0
    scored.sort(key=lambda s: -s["score"])
    return scored, complete


def canon_scores(artist_mbid: str, rgs: list[dict]) -> list[dict]:
    """Normalized essentiality per release-group given the artist's RGs.
    Returns [] if any ListenBrainz batch failed so partial batches do not
    poison the 7-day canon cache."""
    scored, complete = canon_scores_checked(artist_mbid, rgs)
    return scored if complete else []

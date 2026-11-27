"""ListenBrainz client — popularity data for essential-albums scoring.

Free, no-auth-needed public endpoints. Used to rank an artist's release-
groups by how much the world actually listens to them, so hunter can tell
"you own 3 of Bowie's 9 essentials" and one-tap fill only the canon gaps.
"""
from __future__ import annotations

import logging
import math

from hunter.config import HunterConfig
from hunter.http_client import (
    HttpError,
    HttpSession,
    PoliteClient,
    parse_retry_after,
)

log = logging.getLogger("hunter.listenbrainz")

LB = "https://api.listenbrainz.org/1"
_TIMEOUT = 12
_MIN_INTERVAL = 1.05
_client = PoliteClient(timeout=_TIMEOUT, min_interval=_MIN_INTERVAL)


def _session(cfg: HunterConfig | None = None) -> HttpSession:
    """Return a per-thread HttpSession with dynamically computed User-Agent."""
    return _client.session(cfg)


def release_group_listens_checked(
    rgids: list[str],
    *,
    cfg: HunterConfig | None = None,
) -> tuple[dict[str, int], bool]:
    """Return ``({rgid: total_listen_count}, complete)`` for a batch of
    release-group MBIDs. ``complete`` is False if any batch failed or returned
    malformed data so callers don't poison caches with partial results."""
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
            _client.limiter.acquire()
            try:
                resp = http.post(
                    f"{LB}/popularity/release-group",
                    json={"release_group_mbids": batch},
                    timeout=_TIMEOUT,
                    raise_for_status=False,
                )
            except HttpError as exc:
                log.warning("LB rg-popularity failed: %s", exc)
                if attempt == 0:
                    _client.limiter.push_back(2.0)
                    continue
                break

            if resp.status_code in (429, 502, 503, 504):
                ra = parse_retry_after(resp.headers.get("Retry-After"), default=2.0, cap=30.0)
                _client.limiter.push_back(ra)
                if attempt == 0:
                    log.warning("LB transient HTTP %d — waiting %.1fs", resp.status_code, ra)
                    continue
                log.warning("LB transient HTTP %d on retry — marking incomplete", resp.status_code)
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
            break
    return out, complete


def canon_scores_checked(
    rgs: list[dict],
    *,
    cfg: HunterConfig | None = None,
) -> tuple[list[dict], bool]:
    """Return ``(scored, complete)`` normalized essentiality per release-group."""
    rgids = [r.get("rgid", "") for r in rgs if isinstance(r, dict)]
    listens, complete = release_group_listens_checked(rgids, cfg=cfg)
    scored = []
    seen: set[str] = set()
    for rg in rgs:
        if not isinstance(rg, dict):
            continue
        rgid = rg.get("rgid")
        if not rgid or rgid in seen:
            continue
        seen.add(rgid)
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


def canon_scores(rgs: list[dict], *, cfg: HunterConfig | None = None) -> list[dict]:
    """Normalized essentiality per release-group given the artist's RGs.
    Returns [] if any ListenBrainz batch failed so partial batches do not
    poison the 7-day canon cache."""
    scored, complete = canon_scores_checked(rgs, cfg=cfg)
    return scored if complete else []

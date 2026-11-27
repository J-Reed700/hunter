"""MusicBrainz client — polite (1 req/s, UA, 503 retry), cached, shared.

Used by the web UI's request desk and by the artist-watch pass phase.
MB is the stack's canonical metadata source (beets tags from it), so
requests and imports speak the same language.
"""
from __future__ import annotations

import copy
import logging
import re
import threading
import time
import urllib.parse
from collections import Counter, OrderedDict

from hunter.config import HunterConfig
from hunter.domain import extract_year
from hunter.http_client import (
    HttpError,
    HttpSession,
    PoliteClient,
    parse_retry_after,
)

log = logging.getLogger("hunter.mb")

MB = "https://musicbrainz.org/ws/2"
MB_TIMEOUT = 12
_MIN_INTERVAL = 1.1
_MAX_ATTEMPTS = 4          # MB sheds load with 503 constantly; ride it out
_FRESH_TTL = 600           # a hit newer than this skips the network entirely
_STALE_TTL = 86_400        # older hits still beat a hard failure (served + logged)

_client = PoliteClient(timeout=MB_TIMEOUT, min_interval=_MIN_INTERVAL)
_cache_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, object]] = OrderedDict()

_LUCENE_SPECIAL_RE = re.compile(r'([+\-!(){}[\]^"~*?:\\/])')
_LUCENE_BOOL_RE = re.compile(r"\b(AND|OR|NOT)\b")


def _user_agent(cfg: HunterConfig | None = None) -> str:
    return _client.user_agent(cfg)


def _session(cfg: HunterConfig | None = None) -> HttpSession:
    """Return a per-thread HttpSession with dynamically computed User-Agent."""
    return _client.session(cfg)


class MBUnavailable(RuntimeError):
    """MB failed AND no cached copy exists — callers degrade gracefully."""


def _escape_lucene(text: str) -> str:
    """Escape Lucene special characters and boolean operators (AND/OR/NOT)."""
    out = _LUCENE_SPECIAL_RE.sub(r"\\\1", text)
    out = out.replace("&&", r"\&\&").replace("||", r"\|\|")
    return _LUCENE_BOOL_RE.sub(lambda m: m.group(1).lower(), out)


def mb_get(path: str, params: dict, *, cfg: HunterConfig | None = None) -> dict:
    """Throttled, retried, cache-backed GET. Never raises on a transient MB
    hiccup when a cached copy (even stale) exists. Raises MBUnavailable only
    when there is genuinely nothing to serve after exhausting retries."""
    key = path + "?" + urllib.parse.urlencode(sorted(params.items()))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _FRESH_TTL:
            _cache.move_to_end(key)
            return copy.deepcopy(hit[1])                 # type: ignore[return-value]

    last_err = ""
    http = _session(cfg)
    for attempt in range(_MAX_ATTEMPTS):
        _client.limiter.acquire()
        try:
            resp = http.get(
                f"{MB}{path}",
                params={**params, "fmt": "json"},
                timeout=MB_TIMEOUT,
                raise_for_status=False,
            )
        except HttpError as exc:
            last_err = str(exc)
            if attempt + 1 < _MAX_ATTEMPTS:
                delay = min(1.5 * (2 ** attempt), 8.0)
                _client.limiter.push_back(delay)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError as exc:
                last_err = f"bad json: {exc}"
                break
            if not isinstance(data, dict):
                last_err = "unexpected non-object json"
                break
            with _cache_lock:
                _cache[key] = (time.monotonic(), data)
                _cache.move_to_end(key)
                while len(_cache) > 800:
                    _cache.popitem(last=False)
            return copy.deepcopy(data)

        # 502/503/504 (gateway/load-shed) and 429 (rate) are retryable.
        if resp.status_code in (429, 502, 503, 504) and attempt + 1 < _MAX_ATTEMPTS:
            ra = parse_retry_after(resp.headers.get("Retry-After"), default=0.0, cap=30.0)
            delay = max(ra, min(1.5 * (2 ** attempt), 30.0))
            _client.limiter.push_back(delay)
            last_err = f"{resp.status_code}"
            continue

        last_err = f"{resp.status_code} {resp.text[:80]}"
        break

    # Exhausted. Fall back to a stale-but-usable cached copy before failing.
    if hit and (time.monotonic() - hit[0]) < _STALE_TTL:
        log.warning("MB %s failed (%s) — serving stale cache", path, last_err)
        return copy.deepcopy(hit[1])                     # type: ignore[return-value]
    raise MBUnavailable(f"MusicBrainz unavailable ({last_err})")


def credit(artist_credit: list | None) -> str:
    """Join an MB artist-credit honoring joinphrases ("A feat. B")."""
    return "".join(
        ((c.get("name") or (c.get("artist") or {}).get("name", ""))
         + (c.get("joinphrase") or ""))
        for c in (artist_credit or [])
        if isinstance(c, dict)
    ).strip()


def rg_year(rg: dict) -> int:
    return extract_year(rg.get("first-release-date"))


def rg_track_count(rgid: str) -> int | None:
    """Most common track total across the release-group's releases."""
    try:
        data = mb_get("/release", {"release-group": rgid, "inc": "media",
                                   "limit": 25})
    except (MBUnavailable, ValueError, KeyError, TypeError):
        return None
    totals = []
    for rel in data.get("releases") or []:
        if not isinstance(rel, dict):
            continue
        n = sum(
            (m.get("track-count") or 0)
            for m in (rel.get("media") or [])
            if isinstance(m, dict)
        )
        if n > 0:
            totals.append(n)
    if not totals:
        return None
    return Counter(totals).most_common(1)[0][0]


_SESSIONISH = ("session", "acoustic", "live", "apple music", "spotify",
               "amazon", "triple j", "radio", "unplugged", "by request")


def _rel_score(rel: dict) -> tuple:
    """Rank a recording's releases toward the canonical studio album."""
    rg = rel.get("release-group") or {}
    title = (rg.get("title") or "").lower()
    return (
        1 if rg.get("secondary-types") else 0,
        0 if (rg.get("primary-type") or "Album") == "Album" else 1,
        0 if rel.get("status") == "Official" else 1,
        1 if any(w in title for w in _SESSIONISH) else 0,
    )


PAGE_SIZE = 20


def search(kind: str, query: str, offset: int = 0) -> list[dict]:
    raw_q = (query or "").strip()
    if not raw_q:
        return []
    q_esc = _escape_lucene(raw_q)

    if kind == "artist":
        data = mb_get("/artist", {"query": q_esc, "limit": 8,
                                  "offset": offset})
        return [{
            "id": a["id"], "name": a.get("name", ""),
            "extra": ", ".join(x for x in (
                a.get("type"), a.get("disambiguation"),
                (a.get("area") or {}).get("name")) if x),
        } for a in (data.get("artists") or []) if isinstance(a, dict) and a.get("id")]

    phrase_esc = raw_q.replace("\\", "\\\\").replace('"', '\\"')
    broad = f'({q_esc}) OR artist:"{phrase_esc}" OR creditname:"{phrase_esc}"'

    if kind == "song":
        data = mb_get("/recording", {"query": broad, "limit": PAGE_SIZE,
                                     "offset": offset})
        out, seen = [], set()
        for rec in data.get("recordings") or []:
            if not isinstance(rec, dict) or rec.get("video"):
                continue
            releases = [
                r for r in (rec.get("releases") or [])
                if isinstance(r, dict) and (r.get("release-group") or {}).get("id")
            ]
            if not releases:
                continue
            rel = min(releases, key=_rel_score)
            rg = rel["release-group"]
            if rg["id"] in seen:
                continue
            seen.add(rg["id"])
            out.append({
                "song": rec.get("title", ""), "rgid": rg["id"],
                "album": rg.get("title", rel.get("title", "")),
                "artist": credit(rec.get("artist-credit")),
                "year": rg_year(rg) or extract_year(rel.get("date")),
                "_clean": _rel_score(rel) == (0, 0, 0, 0),
            })
        out.sort(key=lambda e: not e.pop("_clean"))   # studio albums first
        return out

    data = mb_get("/release-group", {"query": broad, "limit": PAGE_SIZE,
                                     "offset": offset})
    return [{
        "rgid": rg["id"], "album": rg.get("title", ""),
        "artist": credit(rg.get("artist-credit")),
        "year": rg_year(rg),
        "type": rg.get("primary-type") or "",
    } for rg in (data.get("release-groups") or [])
        if isinstance(rg, dict) and rg.get("id")
        and (rg.get("primary-type") or "Album") in ("Album", "EP", "Single")]


def artist_release_groups(artist_id: str) -> list[dict]:
    """Album/EP release-groups for an artist (paginated up to 500),
    studio-first then newest."""
    rgs: list[dict] = []
    seen_ids: set[str] = set()
    for offset in range(0, 500, 100):
        data = mb_get(
            "/release-group",
            {"artist": artist_id, "type": "album|ep", "limit": 100, "offset": offset},
        )
        page = data.get("release-groups") or []
        for rg in page:
            if not isinstance(rg, dict):
                continue
            rgid = rg.get("id")
            if not rgid or rgid in seen_ids:
                continue
            seen_ids.add(rgid)
            rgs.append({
                "rgid": rgid,
                "album": rg.get("title", ""),
                "year": rg_year(rg),
                "date": rg.get("first-release-date") or "",
                "type": rg.get("primary-type") or "",
                "secondary": rg.get("secondary-types") or [],
            })
        if len(page) < 100:
            break
    else:
        log.info("MB artist_release_groups(%s) hit 500-RG cap", artist_id)
    rgs.sort(key=lambda r: (bool(r["secondary"]), -(r["year"] or 0)))
    return rgs

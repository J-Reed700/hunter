"""Recovery — reconciliation between the beets library and the files on disk.

beets is the source of truth for what the library *contains*; the mounted
library directory is the truth for what still *exists*. Every item row whose
file is gone is a loss, and this module is the only place that says so. That
framing matters: diffing raw download history against the library would count
rejected downloads as lost.

Scoping it to the library instead makes the number exact: 0 means 0.

⚠ THE MOUNT GUARD IS LOAD-BEARING. If the library mount is unavailable, every
path stats false and the honest answer becomes "you lost your entire library"
— which a "recover all" would turn into massive re-downloads. So a scan that
can't positively verify the mount, or that finds an implausible share of the
library missing, is reported as UNTRUSTED and cannot be re-requested. Read the
whole subtree before you believe it is empty.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

from hunter import domain, libquery, storage
from hunter.config import HunterConfig

log = logging.getLogger(__name__)

_CACHE: dict[str, object] = {}
_CACHE_TTL = 300.0
_LOCK = threading.Lock()
_INFLIGHT: tuple[threading.Event, dict[str, dict | None]] | None = None

# Above this share of the library missing we assume a broken mount, not a
# deletion. A real accident takes a slice; an unmounted disk takes everything.
_SUSPECT_FRACTION = 0.25
# Canary files: if these sampled library paths are all absent the mount is gone.
_CANARY_N = 40
_BATCH_SIZE = 2000

_SORTS = {
    "artist": lambda t: (t["albumartist"].lower(), t["album"].lower(), t["disc"], t["track"]),
    "album": lambda t: (t["album"].lower(), t["disc"], t["track"]),
    "title": lambda t: t["title"].lower(),
    "added": lambda t: -t["added"],
    "size": lambda t: -t["bytes"],
}


def _text(v) -> str:
    fn = getattr(libquery, "text", None) or getattr(libquery, "_text", None)
    if callable(fn):
        return fn(v)
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "surrogateescape")
    return str(v)


def _est_bytes(length: float, bitrate: int, fmt: str) -> int:
    """beets keeps no size column; reconstruct it from duration x bitrate."""
    if length and bitrate:
        return int(length * bitrate / 8)
    if length:  # unknown bitrate — assume a middling lossless/lossy stream
        return (
            int(length * 900_000 / 8)
            if fmt.upper() in domain.LOSSLESS_FORMATS
            else int(length * 256_000 / 8)
        )
    return 0


def _storage_ok(cfg: HunterConfig) -> tuple[bool, str]:
    """Verify mount liveness and fstype via storage.summary/probe."""
    if hasattr(cfg, "quarantine_dir"):
        sg = storage.summary(cfg)
        if not sg.get("ok"):
            return False, str(sg.get("error") or "storage check failed")
    else:
        eff = getattr(cfg, "storage_fstype", None)
        p = storage.probe(cfg.music_root)
        why = p.reason(eff)
        if why:
            return False, f"merged: {why}"
    root = cfg.music_root
    try:
        if not root.is_dir():
            return False, f"{root} is not a directory — the library mount is not available"
        if not any(root.iterdir()):
            return False, f"{root} is empty — the library mount is not available"
    except OSError as exc:
        return False, f"{root} is unreadable ({exc}) — the library mount is not available"
    return True, ""


def _run_scan(cfg: HunterConfig) -> dict:
    ok, why = _storage_ok(cfg)
    tracks: list[dict] = []
    album_totals: dict[int, int] = {}
    total = 0
    alive = 0
    db_error = ""
    mount_err = ""

    if ok:
        conn: sqlite3.Connection | None = None
        try:
            conn = libquery.connect(cfg)
            cur = conn.execute(
                "SELECT i.id, i.path, i.album_id, i.artist, i.albumartist, i.album,"
                "       i.title, i.track, i.disc, i.year, i.format, i.bitrate,"
                "       i.length, i.added, i.mb_albumid, i.mb_trackid"
                "  FROM items i"
            )
            while True:
                batch = cur.fetchmany(_BATCH_SIZE)
                if not batch:
                    break
                for r in batch:
                    total += 1
                    aid = r[2] or 0
                    if aid:
                        album_totals[aid] = album_totals.get(aid, 0) + 1
                    hp = domain.container_to_host_path(cfg, r[1])
                    if hp is not None:
                        try:
                            os.stat(hp)
                            alive += 1
                            continue
                        except FileNotFoundError:
                            pass
                        except OSError as exc:
                            mount_err = f"mount error reading {hp}: {exc}"
                            break
                    fmt = _text(r[10])
                    tracks.append({
                        "id": r[0],
                        "album_id": aid,
                        "artist": _text(r[3]),
                        "albumartist": _text(r[4]) or _text(r[3]),
                        "album": _text(r[5]),
                        "title": _text(r[6]) or "(untitled)",
                        "track": r[7] or 0,
                        "disc": r[8] or 0,
                        "year": r[9] or 0,
                        "format": fmt,
                        "bitrate": r[11] or 0,
                        "length": r[12] or 0.0,
                        "added": r[13] or 0.0,
                        "mb_albumid": _text(r[14]),
                        "mb_trackid": _text(r[15]),
                        "path": _text(r[1]),
                        "bytes": _est_bytes(r[12] or 0.0, r[11] or 0, fmt),
                    })
                if mount_err:
                    break
        except sqlite3.Error as exc:
            db_error = str(exc)
            log.warning("recovery: beets read failed: %s", exc)
        finally:
            if conn is not None:
                conn.close()

    if ok and not db_error and not mount_err and total > 0 and alive == 0:
        mount_err = (
            f"none of {total} library files are readable — "
            "treating this as a mount failure, not a deletion"
        )

    for t in tracks:
        t["album_track_total"] = album_totals.get(t["album_id"], 0) if t["album_id"] else 0

    frac = (len(tracks) / total) if total else 0.0
    suspect = (
        (not ok)
        or bool(db_error)
        or bool(mount_err)
        or (frac > _SUSPECT_FRACTION and len(tracks) > 200)
    )
    reason = (
        why
        or (f"beets DB read failed: {db_error}" if db_error else "")
        or mount_err
        or (
            f"{frac * 100:.0f}% of the library is unreadable — that is a mount or "
            "permissions failure, not a deletion"
            if suspect
            else ""
        )
    )
    if suspect:
        log.warning("recovery: %s", reason)

    return {
        "scanned_ts": time.time(),
        "items": total,
        "missing": 0 if suspect else len(tracks),
        "bytes": 0 if suspect else sum(t["bytes"] for t in tracks),
        "tracks": [] if suspect else tracks,
        "trusted": not suspect,
        "untrusted_reason": reason,
        "music_root": str(cfg.music_root),
    }


def scan(cfg: HunterConfig, *, refresh: bool = False) -> dict:
    """Stat every item in the library without holding _LOCK across the scan."""
    global _INFLIGHT
    while True:
        with _LOCK:
            hit = _CACHE.get("scan")
            if hit and not refresh and time.time() - hit["scanned_ts"] < _CACHE_TTL:  # type: ignore[index]
                return hit  # type: ignore[return-value]
            inflight = _INFLIGHT
            if inflight is None:
                evt = threading.Event()
                holder: dict[str, dict | None] = {"res": None}
                _INFLIGHT = (evt, holder)
                break
        evt_wait, holder_wait = inflight
        evt_wait.wait()
        if holder_wait["res"] is not None:
            return holder_wait["res"]
        refresh = False

    res: dict | None = None
    try:
        res = _run_scan(cfg)
    finally:
        with _LOCK:
            holder["res"] = res
            if res is not None and res.get("trusted"):
                _CACHE["scan"] = res
            else:
                _CACHE.pop("scan", None)
            _INFLIGHT = None
            evt.set()
    return res


def albums_of(tracks: list[dict]) -> list[dict]:
    """Roll missing tracks up to the album they belong to."""
    by: dict[str, dict] = {}
    for t in tracks:
        key = f"a{t['album_id']}" if t["album_id"] else f"n:{t['albumartist']}|{t['album']}".lower()
        g = by.get(key)
        if g is None:
            g = by[key] = {
                "key": key,
                "album_id": t["album_id"],
                "artist": t["albumartist"],
                "album": t["album"] or "(no album)",
                "year": t["year"],
                "tracks": 0,
                "album_track_total": 0,
                "bytes": 0,
                "item_ids": [],
            }
        g["tracks"] += 1
        g["album_track_total"] = max(
            g["album_track_total"], int(t.get("album_track_total") or 0)
        )
        g["bytes"] += t["bytes"]
        g["item_ids"].append(t["id"])
    return sorted(by.values(), key=lambda g: (g["artist"].lower(), g["album"].lower()))


def _match(t: dict, q: str) -> bool:
    return not q or q in f"{t['albumartist']} {t['artist']} {t['album']} {t['title']}".lower()


def query(cfg: HunterConfig, *, q: str = "", sort: str = "artist", album_id: int = 0,
          artist: str = "", fmt: str = "", offset: int = 0, limit: int = 100,
          refresh: bool = False) -> dict:
    """Paged view of the missing tracks, plus their album rollup."""
    res = scan(cfg, refresh=refresh)
    ql, al = q.strip().lower(), artist.strip().lower()
    hits = [t for t in res["tracks"]
            if _match(t, ql)
            and (not album_id or t["album_id"] == album_id)
            and (not al or t["albumartist"].lower() == al)
            and (not fmt or t["format"].upper() == fmt.upper())]
    hits.sort(key=_SORTS.get(sort, _SORTS["artist"]))
    albums = albums_of(hits)
    return {
        "tracks": hits[offset:offset + limit],
        "albums": albums[:400],
        "total": len(hits),
        "albums_total": len(albums),
        "bytes": sum(t["bytes"] for t in hits),
        "library_items": res["items"],
        "missing": res["missing"],
        "library_bytes": res["bytes"],
        "scanned_ts": res["scanned_ts"],
        "trusted": res["trusted"],
        "untrusted_reason": res["untrusted_reason"],
        "music_root": res["music_root"],
        "formats": sorted({t["format"] for t in res["tracks"] if t["format"]}),
    }


def keys(cfg: HunterConfig, cap: int = 20000, **kw) -> dict:
    """Every matching item id, for 'select all N matching'."""
    kw.pop("offset", None), kw.pop("limit", None)
    hits = query(cfg, offset=0, limit=10 ** 9, **kw)
    ids = [t["id"] for t in hits["tracks"]]
    return {"item_ids": ids[:cap], "total": len(ids), "capped": len(ids) > cap}


def verify(cfg: HunterConfig, item_ids: list[int]) -> list[dict]:
    """Re-stat the chosen ids at request time — never act on a cached absence
    or an unhealthy mount."""
    if not item_ids:
        return []
    ok, why = _storage_ok(cfg)
    if not ok:
        log.warning("recovery verify aborted — mount check failed: %s", why)
        with _LOCK:
            _CACHE.pop("scan", None)
        return []

    wanted = sorted({int(i) for i in item_ids})
    rows: list = []
    album_totals: dict[int, int] = {}
    conn: sqlite3.Connection | None = None
    try:
        conn = libquery.connect(cfg)
        for chunk in domain.chunked(wanted, 400):
            qmarks = ",".join("?" * len(chunk))
            rows.extend(
                conn.execute(
                    "SELECT id, path, album_id, albumartist, artist, album, title, year, track"
                    f"  FROM items WHERE id IN ({qmarks})",
                    chunk,
                ).fetchall()
            )
        aids = sorted({int(r[2]) for r in rows if r[2]})
        for chunk in domain.chunked(aids, 400):
            qmarks = ",".join("?" * len(chunk))
            for ar in conn.execute(
                f"SELECT album_id, COUNT(*) FROM items WHERE album_id IN ({qmarks}) GROUP BY album_id",
                chunk,
            ).fetchall():
                album_totals[int(ar[0])] = int(ar[1])
    except sqlite3.Error as exc:
        log.warning("recovery verify DB read failed: %s", exc)
        return []
    finally:
        if conn is not None:
            conn.close()

    out: list[dict] = []
    for r in rows:
        hp = domain.container_to_host_path(cfg, r[1])
        if hp is not None:
            try:
                os.stat(hp)
                continue  # it came back — nothing to recover
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("recovery verify aborted — stat error on %s: %s", hp, exc)
                with _LOCK:
                    _CACHE.pop("scan", None)
                return []
        aid = r[2] or 0
        out.append({
            "id": r[0],
            "album_id": aid,
            "albumartist": _text(r[3]) or _text(r[4]),
            "album": _text(r[5]),
            "title": _text(r[6]),
            "year": r[7] or 0,
            "track": r[8] or 0,
            "album_track_total": album_totals.get(aid, 0) if aid else 0,
            "bytes": 0,
            "artist": _text(r[4]),
            "format": "",
            "disc": 0,
        })
    return out

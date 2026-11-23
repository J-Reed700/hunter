"""Shared helpers for route modules: the owned-oracle cache, state/DB
context managers, zombie-free pass spawning, and async MB track-count backfill.
"""
from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from typing import Any

import requests

from hunter import http_client, library as library_mod, mb
from hunter.config import HunterConfig
from hunter.jobs import Jobs
from hunter.library import BeetsLibrary
from hunter.state import HunterState

log = logging.getLogger("hunter.webui")

ART_SESSION = requests.Session()
ART_SESSION.headers["User-Agent"] = http_client.default_user_agent(None, "hunter-art")

_children_lock = threading.Lock()
_children: set[subprocess.Popen] = set()


def library(cfg: HunterConfig) -> BeetsLibrary | None:
    return library_mod.get_cached_library(cfg)


def invalidate_library() -> None:
    library_mod.invalidate_library_cache()


def already_have(
    cfg: HunterConfig,
    artist: str,
    album: str,
    *,
    rgid: str | None = None,
    total: int | None = None,
):
    lib = library(cfg)
    if lib is None:
        return None                        # fail OPEN
    return lib.already_have(
        artist,
        album,
        rgid=rgid,
        expected_tracks=total,
        min_coverage=cfg.owned_coverage,
    )


def owned_payload(res) -> dict[str, Any]:
    return {
        "owned": True,
        "have": res.item_count,
        "expected": res.expected,
        "album_id": res.album_id,
    }


def _extract_rgid(row: dict[str, Any]) -> str | None:
    rgid = row.get("rgid")
    if rgid:
        return str(rgid)
    key = str(row.get("key") or row.get("spotify_album_id") or "")
    for prefix in ("request:rg:", "release:rg:"):
        if key.startswith(prefix):
            return key[len(prefix):]
    return None


def annotate_owned(
    cfg: HunterConfig,
    results: list[dict[str, Any]],
    count_key: str = "tracks",
) -> list[dict[str, Any]]:
    lib = library(cfg)
    if lib is None:
        return results
    memo: dict[tuple[str | None, str, str, int | None], Any] = {}
    for r in results:
        expected = r.get(count_key) or None
        rgid = _extract_rgid(r)
        artist = r.get("artist", "")
        album = r.get("album", "")
        mkey = (rgid, artist, album, expected)
        if mkey not in memo:
            res = None
            if rgid:
                res = lib.rg_owned(rgid, expected, cfg.owned_coverage)
            if res is None:
                res = lib.owned(artist, album, expected, cfg.owned_coverage)
            memo[mkey] = res
        res = memo[mkey]
        r["owned"] = res.owned
        r["have"] = res.item_count
        r["expected"] = res.expected
        r["album_id"] = res.album_id
    return results


def annotate_sp_owned(
    cfg: HunterConfig,
    rows: list[dict[str, Any]],
    album_key: str = "album",
) -> list[dict[str, Any]]:
    """Ownership for Spotify-cache rows, memoized per ``(artist, album, expected)``."""
    lib = library(cfg)
    if lib is None:
        return rows
    cache: dict[tuple[str, str, int | None], Any] = {}
    for r in rows:
        expected = r.get("album_total") or r.get("total_tracks") or None
        key = (r.get("artist") or "", r.get(album_key) or "", expected)
        if key not in cache:
            cache[key] = lib.owned(
                key[0], key[1], expected, cfg.owned_coverage
            )
        res = cache[key]
        r["owned"] = res.owned
        r["have"] = res.item_count
        r["expected"] = res.expected
        r["beets_album_id"] = res.album_id
    return rows


@contextmanager
def state(cfg: HunterConfig):
    st = HunterState(cfg.state_db)
    try:
        yield st
    finally:
        st.close()


def jobs(cfg: HunterConfig) -> Jobs:
    return Jobs(cfg)


def _reap_proc(proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    except Exception:                      # noqa: BLE001
        pass
    finally:
        with _children_lock:
            _children.discard(proc)


def reap_children() -> int:
    """Poll tracked spawned child processes and discard exited ones."""
    with _children_lock:
        done = [p for p in _children if p.poll() is not None]
        for p in done:
            _children.discard(p)
        return len(done)


def spawn_pass(*extra: str) -> subprocess.Popen | None:
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "hunter.cli", "pass", *extra]
        )
    except OSError as exc:
        log.warning("failed to spawn pass: %s", exc)
        return None
    with _children_lock:
        _children.add(proc)
    threading.Thread(
        target=_reap_proc, args=(proc,), daemon=True, name="pass-reaper"
    ).start()
    return proc


def fetch_counts_async(
    cfg: HunterConfig, pairs: list[tuple[str, str]]
) -> None:
    """Backfill MB track counts after responding without blocking the request."""
    mb_exc = getattr(mb, "MBUnavailable", RuntimeError)

    def work() -> None:
        for key, rgid in pairs:
            try:
                tracks = mb.rg_track_count(rgid)
            except (mb_exc, requests.RequestException, ValueError) as exc:
                log.debug("count backfill skipped for %s: %s", rgid, exc)
                continue
            if tracks:
                try:
                    with state(cfg) as st:
                        st.update_track_count(key, tracks)
                except (sqlite3.Error, OSError) as exc:
                    log.debug("count backfill save failed for %s: %s", key, exc)

    threading.Thread(
        target=work, daemon=True, name="count-backfill"
    ).start()


def want_status_map(st: HunterState, keys: list[str]) -> dict[str, dict[str, Any]]:
    return st.wants_status_map(keys)

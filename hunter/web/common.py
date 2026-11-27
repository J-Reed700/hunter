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

from hunter import domain, http_client, libops, library as library_mod, mb
from hunter.config import HunterConfig
from hunter.jobs import Jobs
from hunter.library import BeetsLibrary
from hunter.state import HunterState

log = logging.getLogger("hunter.webui")

ART_CLIENT = http_client.PoliteClient(
    component="hunter-art", timeout=10.0, min_interval=0.0
)

_JOB_FINALIZERS = {"library": libops.on_library_job}

_children_lock = threading.Lock()
_children: set[subprocess.Popen] = set()
_children_args: dict[subprocess.Popen, tuple[str, ...]] = {}


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
    return domain.extract_rgid_from_key(key)


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
            memo[mkey] = lib.lookup_owned(
                artist,
                album,
                rgid=rgid,
                expected_tracks=expected,
                min_coverage=cfg.owned_coverage,
            )
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
            cache[key] = lib.lookup_owned(
                key[0],
                key[1],
                expected_tracks=expected,
                min_coverage=cfg.owned_coverage,
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
    return Jobs(cfg, finalizers=_JOB_FINALIZERS)


def _reap_proc(proc: subprocess.Popen) -> None:
    try:
        proc.wait()
    except Exception:                      # noqa: BLE001
        pass
    finally:
        with _children_lock:
            _children.discard(proc)
            _children_args.pop(proc, None)


def tracked_child_pids() -> set[int]:
    """PIDs of live background pass subprocesses tracked by the web layer."""
    with _children_lock:
        return {p.pid for p in _children if p.poll() is None}


def reap_children() -> int:
    """Poll tracked spawned child processes and discard exited ones."""
    with _children_lock:
        done = [p for p in _children if p.poll() is not None]
        for p in done:
            _children.discard(p)
            _children_args.pop(p, None)
        return len(done)


def spawn_pass(*extra: str) -> subprocess.Popen | None:
    key = tuple(extra)
    with _children_lock:
        for p in list(_children):
            if p.poll() is not None:
                _children.discard(p)
                _children_args.pop(p, None)
                continue
            if _children_args.get(p) == key:
                return p
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "hunter.cli", "pass", *extra]
        )
    except OSError as exc:
        log.warning("failed to spawn pass: %s", exc)
        return None
    with _children_lock:
        _children.add(proc)
        _children_args[proc] = key
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

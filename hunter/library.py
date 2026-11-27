"""beets DB matching — the "do we already own this?" oracle.

Read-only snapshot against musiclibrary.db (WAL: safe while beets runs).
Loads the full album/track-total/lossless index in __init__ and closes the
SQLite handle immediately in try/finally so callers never hold or touch a
closed DB handle.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunter.db import open_readonly_db
from hunter.domain import LOSSLESS_SQL, norm_album, norm_artist

log = logging.getLogger("hunter.library")


def has_items_column(conn: sqlite3.Connection, col: str) -> bool:
    """Return True when *col* exists on the beets ``items`` table."""
    return col in {r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()}


def effective_track_total(
    known_total: int,
    item_count: int,
    present_discs: int,
    disctotal: int,
) -> int:
    """Disc-aware expected track total accounting for missing whole discs."""
    if disctotal > 1 and 0 < present_discs < disctotal:
        missing_discs = disctotal - present_discs
        base = known_total if known_total > 0 else item_count
        per_disc = max(1, round(base / present_discs))
        return max(known_total, base + missing_discs * per_disc)
    return known_total


def album_totals_cte(has_disctotal: bool) -> str:
    """SQLite CTE computing per-album ``tracktotal`` across all discs, including
    an estimate for entirely absent discs when ``disctotal > present_discs``."""
    if not has_disctotal:
        return (
            "WITH disc_totals AS ("
            "  SELECT album_id, COALESCE(NULLIF(disc, 0), 1) AS d,"
            "         COALESCE(MAX(tracktotal), 0) AS disc_total"
            "  FROM items WHERE album_id IS NOT NULL"
            "  GROUP BY album_id, COALESCE(NULLIF(disc, 0), 1)"
            "), album_totals AS ("
            "  SELECT album_id, SUM(disc_total) AS tracktotal"
            "  FROM disc_totals GROUP BY album_id"
            ")"
        )
    return (
        "WITH disc_totals AS ("
        "  SELECT album_id, COALESCE(NULLIF(disc, 0), 1) AS d,"
        "         COUNT(id) AS disc_items,"
        "         COALESCE(MAX(tracktotal), 0) AS disc_total,"
        "         COALESCE(MAX(disctotal), 0) AS max_dt"
        "  FROM items WHERE album_id IS NOT NULL"
        "  GROUP BY album_id, COALESCE(NULLIF(disc, 0), 1)"
        "), album_disc_agg AS ("
        "  SELECT album_id,"
        "         SUM(disc_total) AS known_total,"
        "         SUM(disc_items) AS item_count,"
        "         COUNT(*) AS present_discs,"
        "         MAX(max_dt) AS disctotal"
        "  FROM disc_totals GROUP BY album_id"
        "), album_totals AS ("
        "  SELECT album_id,"
        "         CASE WHEN disctotal > 1 AND present_discs > 0 AND present_discs < disctotal THEN"
        "           MAX(known_total,"
        "               (CASE WHEN known_total > 0 THEN known_total ELSE item_count END)"
        "               + (disctotal - present_discs)"
        "                 * MAX(1, CAST(ROUND(1.0 * (CASE WHEN known_total > 0 THEN known_total ELSE item_count END) / present_discs) AS INTEGER)))"
        "         ELSE known_total END AS tracktotal"
        "  FROM album_disc_agg"
        ")"
    )


@dataclass(frozen=True)
class OwnedResult:
    owned: bool
    album_id: int | None
    item_count: int
    coverage: float
    expected: int          # best-known track total (caller's, else album's; 0 = unknown)


def _evaluate_matches(
    matches: list[tuple[int, int, int]],
    expected_tracks: int | None,
    min_coverage: float,
) -> OwnedResult:
    if not matches:
        return OwnedResult(False, None, 0, 0.0, expected_tracks or 0)
    total_items = sum(m[1] for m in matches)
    primary_id = max(matches, key=lambda m: (m[1], m[0]))[0]
    inferred_total = max((m[2] for m in matches), default=0)
    expected = expected_tracks or inferred_total
    if not expected:
        return OwnedResult(
            total_items > 0,
            primary_id,
            total_items,
            1.0 if total_items else 0.0,
            0,
        )
    coverage = total_items / expected
    return OwnedResult(
        coverage >= min_coverage,
        primary_id,
        total_items,
        coverage,
        expected,
    )


class BeetsLibrary:
    def __init__(self, db_path: Path):
        self._albums_by_id: dict[int, tuple[int, int]] = {}
        self._lossless_by_album_id: dict[int, int] = {}
        self._by_key: dict[tuple[str, str], list[tuple[int, int, int]]] = {}
        self._by_rgid: dict[str, list[tuple[int, int, int]]] = {}

        conn = open_readonly_db(db_path, timeout=15.0)
        try:
            self._load(conn)
        finally:
            conn.close()

    def _load(self, conn: sqlite3.Connection) -> None:
        has_dt = has_items_column(conn, "disctotal")
        cte = album_totals_cte(has_dt)
        rows = conn.execute(
            f"{cte} "
            f"SELECT a.id, a.albumartist, a.album, a.mb_releasegroupid,"
            f"       COUNT(i.id) AS item_count,"
            f"       COALESCE(MAX(at.tracktotal), 0) AS tracktotal,"
            f"       SUM(CASE WHEN UPPER(COALESCE(i.format, '')) IN {LOSSLESS_SQL}"
            f"           THEN 1 ELSE 0 END) AS lossless_count"
            f" FROM albums a"
            f" LEFT JOIN items i ON i.album_id = a.id"
            f" LEFT JOIN album_totals at ON at.album_id = a.id"
            f" GROUP BY a.id"
        ).fetchall()

        for album_id, artist, album, rgid, items, tracktotal, lossless in rows:
            ic = int(items or 0)
            tt = int(tracktotal or 0)
            self._albums_by_id[album_id] = (ic, tt)
            self._lossless_by_album_id[album_id] = int(lossless or 0)

            na = norm_artist(artist or "")
            nb = norm_album(album or "")
            if na and nb:
                self._by_key.setdefault((na, nb), []).append((album_id, ic, tt))
            if rgid:
                self._by_rgid.setdefault(rgid, []).append((album_id, ic, tt))

    def __enter__(self) -> "BeetsLibrary":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def rg_owned(
        self,
        rgid: str,
        expected_tracks: int | None,
        min_coverage: float,
    ) -> OwnedResult | None:
        """Ownership by MusicBrainz release-group id; None when absent."""
        if not rgid:
            return None
        matches = self._by_rgid.get(rgid)
        if not matches:
            return None
        return _evaluate_matches(matches, expected_tracks, min_coverage)

    def owned(
        self,
        artist: str,
        album: str,
        expected_tracks: int | None,
        min_coverage: float,
    ) -> OwnedResult:
        """Partial-aware ownership across all matching split album rows."""
        key = (norm_artist(artist), norm_album(album))
        if not key[0] or not key[1]:
            return OwnedResult(False, None, 0, 0.0, expected_tracks or 0)
        matches = self._by_key.get(key, [])
        return _evaluate_matches(matches, expected_tracks, min_coverage)

    def lookup_owned(
        self,
        artist: str,
        album: str,
        *,
        rgid: str | None = None,
        expected_tracks: int | None = None,
        min_coverage: float = 0.8,
    ) -> OwnedResult:
        """Canonical ownership lookup: rgid first, falling back to normalized
        (artist, album) whenever rgid is absent OR only a partial match."""
        rg_hit = (
            self.rg_owned(rgid, expected_tracks, min_coverage)
            if rgid
            else None
        )
        if rg_hit and rg_hit.owned:
            return rg_hit
        name_hit = self.owned(artist, album, expected_tracks, min_coverage)
        if name_hit.owned:
            return name_hit
        if rg_hit is not None and (
            rg_hit.item_count > name_hit.item_count
            or (rg_hit.item_count == name_hit.item_count and rg_hit.album_id is not None)
        ):
            return rg_hit
        return name_hit

    def already_have(
        self,
        artist: str,
        album: str,
        *,
        rgid: str | None = None,
        expected_tracks: int | None = None,
        min_coverage: float = 0.8,
    ) -> OwnedResult | None:
        """Add-time ownership gate: rgid identity first, then normalized name."""
        hit = self.lookup_owned(
            artist,
            album,
            rgid=rgid,
            expected_tracks=expected_tracks,
            min_coverage=min_coverage,
        )
        return hit if hit.owned else None

    def has_album_key(self, artist: str, album: str) -> bool:
        """Return True if any album row matches normalized ``(artist, album)``."""
        key = (norm_artist(artist), norm_album(album))
        return bool(key[0] and key[1] and key in self._by_key)

    def artist_album_counts(self) -> dict[str, int]:
        """Map normalized artist name -> total album row count in beets."""
        counts: dict[str, int] = {}
        for (a, _b), rows_ in self._by_key.items():
            counts[a] = counts.get(a, 0) + len(rows_)
        return counts

    def album_coverage(
        self,
        album_id: int,
        expected_tracks: int | None,
        min_coverage: float,
    ) -> OwnedResult | None:
        """Coverage of one specific beets album row from the loaded snapshot."""
        entry = self._albums_by_id.get(album_id)
        if entry is None or entry[0] == 0:
            return None
        items, tracktotal = entry
        expected = expected_tracks or tracktotal
        if not expected:
            return OwnedResult(
                items > 0, album_id, items, 1.0 if items else 0.0, 0
            )
        coverage = items / expected
        return OwnedResult(
            coverage >= min_coverage, album_id, items, coverage, expected
        )

    def lossless_count(self, album_id: int) -> int:
        """Lossless track count for *album_id* from the loaded snapshot."""
        return self._lossless_by_album_id.get(album_id, 0)

    def close(self) -> None:
        """No-op (SQLite connection is closed immediately after loading snapshot)."""
        return None


class LibraryCache:
    """Thread-safe TTL cache for ``BeetsLibrary`` snapshots."""

    def __init__(self, default_ttl_s: float = 120.0):
        self._lock = threading.Lock()
        self._default_ttl_s = default_ttl_s
        self._entry: tuple[Path, float, BeetsLibrary] | None = None

    def get(self, cfg: Any, ttl_s: float | None = None) -> BeetsLibrary | None:
        db_path = Path(cfg.beets_db)
        ttl = self._default_ttl_s if ttl_s is None else float(ttl_s)
        now = time.monotonic()
        with self._lock:
            if self._entry is not None:
                cached_path, cached_ts, lib = self._entry
                if cached_path == db_path and (now - cached_ts) < ttl:
                    return lib
            try:
                lib = BeetsLibrary(db_path)
            except (sqlite3.Error, OSError) as exc:
                log.warning("failed to load beets library %s: %s", db_path, exc)
                return None
            self._entry = (db_path, time.monotonic(), lib)
            return lib

    def invalidate(self) -> None:
        with self._lock:
            self._entry = None


library_cache = LibraryCache()


def get_cached_library(cfg: Any, ttl_s: float = 120.0) -> BeetsLibrary | None:
    return library_cache.get(cfg, ttl_s=ttl_s)


def invalidate_library_cache() -> None:
    library_cache.invalidate()


__all__ = [
    "BeetsLibrary",
    "LibraryCache",
    "OwnedResult",
    "album_totals_cte",
    "effective_track_total",
    "get_cached_library",
    "has_items_column",
    "invalidate_library_cache",
    "library_cache",
]

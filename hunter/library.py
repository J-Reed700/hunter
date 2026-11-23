"""beets DB matching — the "do we already own this?" oracle.

Read-only snapshot against musiclibrary.db (WAL: safe while beets runs).
Loads the full album/track-total/lossless index in __init__ and closes the
SQLite handle immediately in try/finally so callers never hold or touch a
closed DB handle.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunter.db import open_readonly_db
from hunter.domain import LOSSLESS_FORMATS, norm_album, norm_artist, normalize

_LOSSLESS_SQL = "(" + ",".join(f"'{fmt}'" for fmt in sorted(LOSSLESS_FORMATS)) + ")"


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
        self._index = self._by_key  # backwards-compat alias
        self._by_rgid: dict[str, list[tuple[int, int, int]]] = {}

        conn = open_readonly_db(db_path, timeout=15.0)
        try:
            self._load(conn)
        finally:
            conn.close()

    def _load(self, conn: sqlite3.Connection) -> None:
        item_cols = {
            r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()
        }
        disctotal_expr = (
            "COALESCE(MAX(i.disctotal), 0)" if "disctotal" in item_cols else "0"
        )
        rows = conn.execute(
            f"WITH disc_totals AS ("
            f"  SELECT album_id, COALESCE(NULLIF(disc, 0), 1) AS d,"
            f"         COALESCE(MAX(tracktotal), 0) AS disc_total"
            f"  FROM items WHERE album_id IS NOT NULL"
            f"  GROUP BY album_id, COALESCE(NULLIF(disc, 0), 1)"
            f"),"
            f"album_totals AS ("
            f"  SELECT album_id, SUM(disc_total) AS tracktotal"
            f"  FROM disc_totals GROUP BY album_id"
            f")"
            f"SELECT a.id, a.albumartist, a.album, a.mb_releasegroupid,"
            f"       COUNT(i.id) AS item_count,"
            f"       COALESCE(MAX(at.tracktotal), 0) AS tracktotal,"
            f"       SUM(CASE WHEN UPPER(COALESCE(i.format, '')) IN {_LOSSLESS_SQL}"
            f"           THEN 1 ELSE 0 END) AS lossless_count,"
            f"       COUNT(DISTINCT CASE WHEN i.id IS NOT NULL"
            f"           THEN COALESCE(NULLIF(i.disc, 0), 1) END) AS present_discs,"
            f"       {disctotal_expr} AS max_disctotal"
            f" FROM albums a"
            f" LEFT JOIN items i ON i.album_id = a.id"
            f" LEFT JOIN album_totals at ON at.album_id = a.id"
            f" GROUP BY a.id"
        ).fetchall()

        for album_id, artist, album, rgid, items, tracktotal, lossless, present_d, max_dt in rows:
            known_total = int(tracktotal or 0)
            ic = int(items or 0)
            present_discs = int(present_d or 0)
            disctotal = int(max_dt or 0)
            if disctotal > 1 and 0 < present_discs < disctotal:
                missing_discs = disctotal - present_discs
                base = known_total if known_total > 0 else ic
                per_disc = max(1, round(base / present_discs))
                tt = max(known_total, base + missing_discs * per_disc)
            else:
                tt = known_total
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
        if rgid:
            hit = self.rg_owned(rgid, expected_tracks, min_coverage)
            if hit and hit.owned:
                return hit
        hit = self.owned(artist, album, expected_tracks, min_coverage)
        return hit if hit.owned else None

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
            except (sqlite3.Error, OSError):
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
    "LOSSLESS_FORMATS",
    "LibraryCache",
    "OwnedResult",
    "get_cached_library",
    "invalidate_library_cache",
    "library_cache",
    "norm_album",
    "norm_artist",
    "normalize",
]

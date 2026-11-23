"""Tests for hunter.library (multi-disc tracktotal, split albums, empty keys, closed-DB safety, LibraryCache)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hunter.library import (
    BeetsLibrary,
    LibraryCache,
    get_cached_library,
    invalidate_library_cache,
)


def _create_beets_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY,
            albumartist TEXT,
            album TEXT,
            mb_releasegroupid TEXT
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            album_id INTEGER,
            disc INTEGER,
            track INTEGER,
            tracktotal INTEGER,
            format TEXT
        );
    """)
    # 1. Multi-disc album (id=1): Disc 1 has 10/10, Disc 2 has 4/8 (total expected = 18, have = 14)
    conn.execute("INSERT INTO albums VALUES (1, 'Pink Floyd', 'The Wall', 'rg-wall')")
    for t in range(1, 11):
        conn.execute("INSERT INTO items (album_id, disc, track, tracktotal, format) VALUES (1, 1, ?, 10, 'FLAC')", (t,))
    for t in range(1, 5):
        conn.execute("INSERT INTO items (album_id, disc, track, tracktotal, format) VALUES (1, 2, ?, 8, 'MP3')", (t,))

    # 2. Split album across two rows (id=2 and id=3): 6 + 6 = 12 tracks, tracktotal=12
    conn.execute("INSERT INTO albums VALUES (2, 'Daft Punk', 'Discovery', 'rg-disc')")
    conn.execute("INSERT INTO albums VALUES (3, 'Daft Punk', 'Discovery (Deluxe Edition)', 'rg-disc')")
    for t in range(1, 7):
        conn.execute("INSERT INTO items (album_id, disc, track, tracktotal, format) VALUES (2, 1, ?, 12, 'FLAC')", (t,))
    for t in range(7, 13):
        conn.execute("INSERT INTO items (album_id, disc, track, tracktotal, format) VALUES (3, 1, ?, 12, 'FLAC')", (t,))

    # 3. Empty/punctuation-only albumartist row (must NOT collide with empty queries!)
    conn.execute("INSERT INTO albums VALUES (4, '!!!', '---', '')")
    conn.execute("INSERT INTO items (album_id, disc, track, tracktotal, format) VALUES (4, 1, 1, 1, 'FLAC')")

    # 4. Live album vs Studio album
    conn.execute("INSERT INTO albums VALUES (5, 'Nirvana', 'MTV Unplugged (Live)', 'rg-live')")
    for t in range(1, 15):
        conn.execute("INSERT INTO items (album_id, disc, track, tracktotal, format) VALUES (5, 1, ?, 14, 'FLAC')", (t,))

    conn.commit()
    conn.close()


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "musiclibrary.db"
        _create_beets_db(self.db_path)
        invalidate_library_cache()

    def tearDown(self):
        invalidate_library_cache()
        self.tmp.cleanup()

    def test_multidisc_tracktotal_and_snapshot_after_close(self):
        lib = BeetsLibrary(self.db_path)
        lib.close()
        # Snapshot methods must work cleanly after close()!
        cov = lib.album_coverage(1, None, 0.8)
        self.assertIsNotNone(cov)
        assert cov is not None
        self.assertEqual(cov.expected, 18)
        self.assertEqual(cov.item_count, 14)
        self.assertAlmostEqual(cov.coverage, 14 / 18)
        self.assertFalse(cov.owned)  # 14/18 = 0.777 < 0.8
        self.assertEqual(lib.lossless_count(1), 10)

    def test_split_album_aggregation_and_empty_key_guard(self):
        lib = BeetsLibrary(self.db_path)
        res = lib.owned("Daft Punk", "Discovery", None, 0.8)
        self.assertTrue(res.owned)
        self.assertEqual(res.item_count, 12)
        self.assertEqual(res.expected, 12)

        rg_res = lib.rg_owned("rg-disc", None, 0.8)
        self.assertIsNotNone(rg_res)
        assert rg_res is not None
        self.assertTrue(rg_res.owned)
        self.assertEqual(rg_res.item_count, 12)

        # Empty-key guard: punctuation-only query must NOT match punctuation-only row
        self.assertFalse(lib.owned("???", "...", None, 0.8).owned)
        # Live album must NOT match studio album query, and matches live query
        self.assertFalse(lib.owned("Nirvana", "MTV Unplugged", None, 0.8).owned)
        self.assertTrue(lib.owned("Nirvana", "MTV Unplugged (Live)", None, 0.8).owned)

    def test_library_cache_never_caches_none_on_error(self):
        bad_cfg = SimpleNamespace(beets_db=Path(self.tmp.name) / "missing.db")
        cache = LibraryCache(default_ttl_s=60.0)
        self.assertIsNone(cache.get(bad_cfg))

        good_cfg = SimpleNamespace(beets_db=self.db_path)
        lib1 = cache.get(good_cfg)
        lib2 = cache.get(good_cfg)
        self.assertIsNotNone(lib1)
        self.assertIs(lib1, lib2)
        cache.invalidate()
        lib3 = cache.get(good_cfg)
        self.assertIsNot(lib1, lib3)

        self.assertIsNotNone(get_cached_library(good_cfg))
        invalidate_library_cache()

    def test_multidisc_missing_entire_disc_not_marked_owned(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("ALTER TABLE items ADD COLUMN disctotal INTEGER")
        conn.execute(
            "INSERT INTO albums (id, albumartist, album, mb_releasegroupid) "
            "VALUES (6, 'Double Artist', 'Double Album', 'rg-double')"
        )
        for tr in range(1, 11):
            conn.execute(
                "INSERT INTO items (album_id, disc, disctotal, track, tracktotal, format) "
                "VALUES (6, 1, 2, ?, 10, 'FLAC')",
                (tr,),
            )
        conn.commit()
        conn.close()

        lib = BeetsLibrary(self.db_path)
        res = lib.owned("Double Artist", "Double Album", None, 0.8)
        self.assertFalse(res.owned)
        self.assertEqual(res.item_count, 10)
        self.assertGreaterEqual(res.expected, 20)
        self.assertLess(res.coverage, 0.8)


if __name__ == "__main__":
    unittest.main()

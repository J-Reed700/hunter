"""Tests for hunter.libquery (path confinement, multi-disc, exact fmt, LIKE escape, overview/junk)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hunter import libquery


def _create_beets_db(db_path: Path, music_root: Path) -> None:
    (music_root / "Artist").mkdir(parents=True)
    track_file = music_root / "Artist" / "01.flac"
    track_file.write_bytes(b"fLaC")
    art_file = music_root / "Artist" / "cover.jpg"
    art_file.write_bytes(b"\xff\xd8\xff")

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY,
            albumartist TEXT,
            albumartist_sort TEXT,
            album TEXT,
            year INTEGER,
            original_year INTEGER,
            genre TEXT,
            albumtype TEXT,
            albumstatus TEXT,
            albumdisambig TEXT,
            catalognum TEXT,
            mb_releasegroupid TEXT,
            mb_albumid TEXT,
            mb_albumartistid TEXT,
            comp INTEGER,
            label TEXT,
            country TEXT,
            artpath BLOB,
            added REAL
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            album_id INTEGER,
            artist TEXT,
            albumartist TEXT,
            title TEXT,
            disc INTEGER,
            disctotal INTEGER,
            track INTEGER,
            tracktotal INTEGER,
            length REAL,
            bitrate INTEGER,
            format TEXT,
            samplerate INTEGER,
            bitdepth INTEGER,
            channels INTEGER,
            genre TEXT,
            bpm INTEGER,
            initial_key TEXT,
            mb_trackid TEXT,
            mb_albumid TEXT,
            acoustid_id TEXT,
            acoustid_fingerprint TEXT,
            path BLOB,
            added REAL,
            comments TEXT,
            lyrics TEXT,
            mtime REAL
        );
    """)
    # Album 1: 2-disc album with 1 track on disc 1 (tracktotal=2) and 1 on disc 2 (tracktotal=2) => 2/4 incomplete!
    conn.execute(
        "INSERT INTO albums VALUES (1, '100% Artist', '100% Artist', 'Double', 2020, 2020,"
        " 'Rock', 'album', '', '', '', 'rg1', '', 'aa1', 0, '', '', ?, 1000.0)",
        (b"/merged/Artist/cover.jpg",),
    )
    conn.execute(
        "INSERT INTO items VALUES (1, 1, '100% Artist', '100% Artist', 'T1', 1, 2, 1, 2,"
        " 200.0, 900000, 'FLAC', 44100, 16, 2, 'Rock', 120, 'C', 'mb1', '', '', '', ?, 1000.0, '', '', 1000.0)",
        (b"/merged/Artist/01.flac",),
    )
    conn.execute(
        "INSERT INTO items VALUES (2, 1, '100% Artist', '100% Artist', 'T2', 2, 2, 1, 2,"
        " 200.0, 320000, 'ALAC', 44100, 16, 2, 'Rock', 120, 'C', 'mb2', '', '', '', ?, 1000.0, '', '', 1000.0)",
        (b"/merged/Artist/02.m4a",),
    )
    # Album 2 by same artist with mb_albumid present -> albums_nombid should be 1, not 2!
    conn.execute(
        "INSERT INTO albums VALUES (2, '100% Artist', '100% Artist', 'Single', 2021, 2021,"
        " 'Rock', 'single', '', '', '', 'rg2', 'mb-alb-2', '', 0, '', '', X'', 1000.0)"
    )
    conn.execute(
        "INSERT INTO items VALUES (3, 2, '100% Artist', '100% Artist', 'S1', 1, 1, 1, 1,"
        " 180.0, 320000, 'AAC', 44100, 16, 2, 'Rock', 120, 'C', 'mb3', 'mb-alb-2', '', '', ?, 1000.0, '', '', 1000.0)",
        (b"/etc/passwd",),
    )
    conn.commit()
    conn.close()


class LibqueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "merged"
        self.q = Path(self.tmp.name) / "quarantine"
        self.q.mkdir()
        self.db_path = Path(self.tmp.name) / "musiclibrary.db"
        _create_beets_db(self.db_path, self.root)
        self.cfg = SimpleNamespace(
            beets_db=self.db_path,
            music_root=self.root,
            quarantine_dir=self.q,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_host_path_strict_confinement(self):
        self.assertEqual(
            libquery.host_path(self.cfg, b"/merged/Artist/01.flac"),
            (self.root / "Artist" / "01.flac").resolve(),
        )
        self.assertIsNone(libquery.host_path(self.cfg, "/etc/passwd"))
        self.assertIsNone(libquery.host_path(self.cfg, "/merged/../../etc/passwd"))

    def test_multidisc_completeness_exact_fmt_and_artist_nombid(self):
        ov = libquery.overview(self.cfg)
        self.assertEqual(ov["incomplete_albums"], 1)
        self.assertEqual(ov["no_art_albums"], 1)

        jk = libquery.junk(self.cfg)
        self.assertEqual(jk["albums"]["incomplete"], 1)
        self.assertEqual(jk["albums"]["onetrack"], 1)

        # Exact format matching: AAC must match Album 2, NOT ALAC on Album 1; MP must match nothing
        res_aac = libquery.albums(self.cfg, fmt="AAC")
        self.assertEqual(res_aac["total"], 1)
        self.assertEqual(res_aac["albums"][0]["id"], 2)
        self.assertEqual(libquery.albums(self.cfg, fmt="MP")["total"], 0)

        # LIKE escaping on 100%
        self.assertEqual(libquery.albums(self.cfg, q="100%")["total"], 2)
        self.assertEqual(libquery.albums(self.cfg, q="100_")["total"], 0)

        # Artist albums_nombid counts distinct albums (1, not 2 tracks!) and deterministic mbid
        ar = libquery.artists(self.cfg)["artists"][0]
        self.assertEqual(ar["albums_nombid"], 1)
        self.assertEqual(ar["mbid"], "aa1")

        # album() checks item existence via confined host_path and reports multi-disc missing tracks
        alb = libquery.album(self.cfg, 1)
        assert alb is not None
        self.assertEqual(alb["tracktotal"], 4)
        self.assertEqual(alb["missing_tracks"], ["1.2", "2.2"])
        self.assertTrue(alb["items"][0]["exists"])
        self.assertFalse(alb["items"][1]["exists"])

        # track() fails closed on /etc/passwd
        tr = libquery.track(self.cfg, 3)
        assert tr is not None
        self.assertFalse(tr["exists"])


if __name__ == "__main__":
    unittest.main()

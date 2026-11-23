from __future__ import annotations

import errno
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.music_root = root / "merged"
        self.quarantine = root / "quarantine"
        (self.music_root / "Music" / "Artist").mkdir(parents=True)
        self.quarantine.mkdir(parents=True)
        self.beets_db = root / "beets.db"
        self.cfg = SimpleNamespace(
            music_root=self.music_root,
            quarantine_dir=self.quarantine,
            beets_db=self.beets_db,
            storage_fstype=None,
        )
        recovery._CACHE.clear()

        self.present = self.music_root / "Music" / "Artist" / "01.flac"
        self.present.write_bytes(b"ok")

        con = sqlite3.connect(self.beets_db)
        con.executescript(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, album_id INTEGER, "
            "artist TEXT, albumartist TEXT, album TEXT, title TEXT, track INTEGER, disc INTEGER, "
            "year INTEGER, format TEXT, bitrate INTEGER, length REAL, added REAL, mb_albumid TEXT, mb_trackid TEXT);"
        )
        con.execute(
            "INSERT INTO items VALUES (1, ?, 10, 'A', 'A', 'Alb', 'T1', 1, 1, 2020, 'FLAC', 900000, 100.0, 1.0, '', '')",
            (b"/merged/Music/Artist/01.flac",),
        )
        con.execute(
            "INSERT INTO items VALUES (2, ?, 10, 'A', 'A', 'Alb', 'T2', 2, 1, 2020, 'FLAC', 900000, 100.0, 1.0, '', '')",
            (b"/merged/Music/Artist/02_missing.flac",),
        )
        con.commit()
        con.close()

    def test_scan_streams_tracks_album_total_and_rolls_up_correctly(self) -> None:
        res = recovery.scan(self.cfg, refresh=True)
        self.assertTrue(res["trusted"])
        self.assertEqual(res["items"], 2)
        self.assertEqual(res["missing"], 1)
        self.assertEqual(res["tracks"][0]["album_track_total"], 2)

        albs = recovery.albums_of(res["tracks"])
        self.assertEqual(len(albs), 1)
        self.assertEqual(albs[0]["tracks"], 1)
        self.assertEqual(albs[0]["album_track_total"], 2)

    def test_enotconn_during_scan_or_verify_aborts_untrusted_and_fails_closed(self) -> None:
        real_stat = recovery.os.stat

        def flaky_stat(path, *a, **kw):
            if "02_missing" in str(path):
                raise OSError(errno.ENOTCONN, "Transport endpoint is not connected")
            return real_stat(path, *a, **kw)

        with mock.patch("hunter.recovery.os.stat", side_effect=flaky_stat):
            res = recovery.scan(self.cfg, refresh=True)
            self.assertFalse(res["trusted"])
            self.assertIn("not connected", res["untrusted_reason"])
            self.assertNotIn("scan", recovery._CACHE)

            verified = recovery.verify(self.cfg, [2])
            self.assertEqual(verified, [])

    def test_sqlite_error_never_cached_as_trusted(self) -> None:
        with mock.patch("hunter.recovery.libquery.connect") as conn_fn:
            conn_fn.return_value.execute.side_effect = sqlite3.OperationalError("locked")
            res = recovery.scan(self.cfg, refresh=True)
        self.assertFalse(res["trusted"])
        self.assertIn("locked", res["untrusted_reason"])
        self.assertNotIn("scan", recovery._CACHE)

    def test_verify_rechecks_storage_summary_and_returns_album_track_total(self) -> None:
        got = recovery.verify(self.cfg, [1, 2])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], 2)
        self.assertEqual(got[0]["album_track_total"], 2)

        with mock.patch("hunter.recovery.storage.summary", return_value={"ok": False, "error": "wrong fstype"}):
            self.assertEqual(recovery.verify(self.cfg, [2]), [])

    def test_missing_beets_db_caught_and_single_flight_shares_untrusted_result(self) -> None:
        bad_cfg = SimpleNamespace(
            music_root=self.music_root,
            quarantine_dir=self.quarantine,
            beets_db=Path(self.tmp.name) / "missing_beets.db",
            storage_fstype=None,
        )
        res = recovery.scan(bad_cfg, refresh=True)
        self.assertFalse(res["trusted"])
        self.assertIn("beets DB read failed", res["untrusted_reason"])
        self.assertEqual(recovery.verify(bad_cfg, [1, 2]), [])

        # Concurrent waiters receive the shared untrusted scan result (no serial retry stampede)
        import threading
        calls = 0

        def slow_untrusted(cfg):
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return {"scanned_ts": time.time(), "items": 0, "missing": 0, "bytes": 0,
                    "tracks": [], "trusted": False, "untrusted_reason": "down", "music_root": ""}

        import time
        recovery._CACHE.clear()
        out: list[dict] = []
        with mock.patch("hunter.recovery._run_scan", side_effect=slow_untrusted):
            threads = [threading.Thread(target=lambda: out.append(recovery.scan(self.cfg))) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(calls, 1)
        self.assertEqual(len(out), 4)
        self.assertTrue(all(not r["trusted"] for r in out))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import downloads


class DownloadsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.music_root = root / "merged"
        self.state_dir = root / "state"
        self.beets_db = root / "beets.db"
        (self.music_root / "Music" / "Artist").mkdir(parents=True)
        self.state_dir.mkdir(parents=True)
        self.cfg = SimpleNamespace(
            music_root=self.music_root,
            quarantine_dir=root / "quarantine",
            state_dir=self.state_dir,
            beets_db=self.beets_db,
            dl_max_bundle_bytes=100 * (1 << 20),
            dl_keep_bytes=200 * (1 << 20),
            dl_ttl_seconds=3600,
            dl_max_files=500,
        )
        self.f1 = self.music_root / "Music" / "Artist" / "01.flac"
        self.f2 = self.music_root / "Music" / "Artist" / "02.flac"
        self.f1.write_bytes(b"audio1" * 100)
        self.f2.write_bytes(b"audio2" * 100)
        # Pre-1980 mtime to test ZIP timestamp clamp
        os.utime(self.f1, (0, 0))

        con = sqlite3.connect(self.beets_db)
        con.executescript(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, album TEXT, year INTEGER, artpath BLOB);"
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT, albumartist TEXT, track INTEGER, disc INTEGER, disctotal INTEGER, format TEXT, album_id INTEGER);"
        )
        con.execute("INSERT INTO albums VALUES (1, 'Album', 2020, X'')")
        # Duplicate titles + None track on multi-disc to test arcname dedupe & track=None safety
        con.execute("INSERT INTO items VALUES (1, ?, 'SameTitle', 'Artist', 'Artist', NULL, 2, 2, 'FLAC', 1)", (b"/merged/Music/Artist/01.flac",))
        con.execute("INSERT INTO items VALUES (2, ?, 'SameTitle', 'Artist', 'Artist', NULL, 2, 2, 'FLAC', 1)", (b"/merged/Music/Artist/02.flac",))
        con.commit()
        con.close()

    def test_resolve_and_build_handles_none_track_pre1980_mtime_and_arc_collisions(self) -> None:
        sel = downloads.resolve(self.cfg, item_ids=[1, 2])
        self.assertEqual(sel["count"], 2)
        self.assertTrue(all("2-00 - SameTitle.flac" in f["arc"] for f in sel["files"]))

        dl_dir = downloads._dir(self.cfg)
        downloads._save(self.cfg, [{"id": "b1", "status": "building", "path": str(dl_dir / "b1.zip")}])
        downloads._write_manifest(self.cfg, "b1", sel["files"])
        downloads._build(self.cfg, "b1")

        b = downloads.get(self.cfg, "b1")
        self.assertEqual(b["status"], "ready")
        self.assertEqual(b["files_done"], 2)
        with zipfile.ZipFile(dl_dir / "b1.zip") as zf:
            names = sorted(zf.namelist())
            self.assertEqual(len(names), 2)
            self.assertNotEqual(names[0], names[1])
            self.assertTrue(any(n.endswith(" (2).flac") for n in names))

    def test_create_enforces_free_space_and_per_user_and_viewer_quotas(self) -> None:
        # Free space guard (< 2GB headroom)
        low_stv = SimpleNamespace(f_bavail=100, f_frsize=4096)
        with mock.patch("hunter.downloads.os.statvfs", return_value=low_stv):
            r = downloads.create(self.cfg, title="t", requested_by="u1", item_ids=[1])
            self.assertFalse(r["ok"])
            self.assertIn("free space", r["error"])

        ok_stv = SimpleNamespace(f_bavail=10_000_000, f_frsize=4096)
        with mock.patch("hunter.downloads.os.statvfs", return_value=ok_stv), \
             mock.patch("hunter.downloads._ensure_worker"):
            r1 = downloads.create(self.cfg, title="t1", requested_by="u1", item_ids=[1])
            r2 = downloads.create(self.cfg, title="t2", requested_by="u1", item_ids=[2])
            r3 = downloads.create(self.cfg, title="t3", requested_by="u1", item_ids=[1])
            self.assertTrue(r1["ok"])
            self.assertTrue(r2["ok"])
            self.assertFalse(r3["ok"])
            self.assertIn("active bundles", r3["error"])

        # Viewer 10GB pending quota
        with mock.patch("hunter.downloads.os.statvfs", return_value=ok_stv), \
             mock.patch("hunter.downloads._ensure_worker"), \
             mock.patch("hunter.downloads.resolve", return_value={
                 "files": [{"id": 1, "src": str(self.f1), "arc": "a.flac", "bytes": 11 * (1 << 30)}],
                 "bytes": 11 * (1 << 30), "count": 1, "missing": 0,
             }):
            big_cfg = SimpleNamespace(**{
                **self.cfg.__dict__,
                "dl_max_bundle_bytes": 20 * (1 << 30),
                "dl_keep_bytes": 30 * (1 << 30),
            })
            rv = downloads.create(big_cfg, title="big", requested_by="viewer1", item_ids=[1], role="viewer")
            self.assertFalse(rv["ok"])
            self.assertIn("viewer", rv["error"])

        # Pending bytes of queued/building bundles subtract from free-space headroom
        downloads._save(self.cfg, [{
            "id": "existing_q", "status": "queued", "requested_by": "other",
            "bytes_total": 9 * (1 << 30), "bytes_done": 0,
        }])
        ten_gb_stv = SimpleNamespace(f_bavail=(10 * (1 << 30)) // 4096, f_frsize=4096)
        with mock.patch("hunter.downloads.os.statvfs", return_value=ten_gb_stv), \
             mock.patch("hunter.downloads._ensure_worker"):
            r_pending = downloads.create(self.cfg, title="t_low", requested_by="u2", item_ids=[1])
            self.assertFalse(r_pending["ok"])
            self.assertIn("free space", r_pending["error"])

        # Viewer total disk cap (20 GB) counts existing ready bundles
        downloads._save(self.cfg, [{
            "id": "ready_1", "status": "ready", "requested_by": "viewer2",
            "bytes_total": 15 * (1 << 30), "bytes_done": 15 * (1 << 30),
        }])
        with mock.patch("hunter.downloads.os.statvfs", return_value=ok_stv), \
             mock.patch("hunter.downloads._ensure_worker"), \
             mock.patch("hunter.downloads.resolve", return_value={
                 "files": [{"id": 1, "src": str(self.f1), "arc": "a.flac", "bytes": 6 * (1 << 30)}],
                 "bytes": 6 * (1 << 30), "count": 1, "missing": 0,
             }):
            big_cfg = SimpleNamespace(**{
                **self.cfg.__dict__,
                "dl_max_bundle_bytes": 20 * (1 << 30),
                "dl_keep_bytes": 30 * (1 << 30),
            })
            rv2 = downloads.create(big_cfg, title="v_over", requested_by="viewer2", item_ids=[1], role="viewer")
            self.assertFalse(rv2["ok"])
            self.assertIn("20 GB", rv2["error"])

    def test_recover_on_startup_marks_building_failed_and_cleans_orphans(self) -> None:
        dl_dir = downloads._dir(self.cfg)
        (dl_dir / "b_stuck.zip.part").write_bytes(b"partial")
        (dl_dir / "orphan.zip").write_bytes(b"orphan")
        (dl_dir / "b_stuck.files.json").write_text("[]")
        downloads._save(self.cfg, [{"id": "b_stuck", "status": "building"}])

        res = downloads.recover_on_startup(self.cfg)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["removed"], 3)
        self.assertEqual(downloads.get(self.cfg, "b_stuck")["status"], "failed")
        self.assertFalse((dl_dir / "orphan.zip").exists())
        self.assertFalse((dl_dir / "b_stuck.zip.part").exists())


if __name__ == "__main__":
    unittest.main()

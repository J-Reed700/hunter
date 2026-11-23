from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
BEETS_AUTO = REPO_ROOT / "src" / "beets" / "automation"
if str(BEETS_AUTO) not in sys.path:
    sys.path.insert(0, str(BEETS_AUTO))

from jobs import blocklist_ctl, library_remove  # noqa: E402
from lib import blocklist  # noqa: E402


def _make_beets_db(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE albums (
                id INTEGER PRIMARY KEY,
                albumartist TEXT,
                album TEXT,
                year INTEGER,
                artpath BLOB
            );
            CREATE TABLE items (
                id INTEGER PRIMARY KEY,
                album_id INTEGER,
                albumartist TEXT,
                artist TEXT,
                album TEXT,
                title TEXT,
                disc INTEGER,
                track INTEGER,
                year INTEGER,
                path BLOB
            );
            CREATE TABLE album_attributes (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER,
                key TEXT,
                value TEXT
            );
            CREATE TABLE item_attributes (
                id INTEGER PRIMARY KEY,
                entity_id INTEGER,
                key TEXT,
                value TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()


class _StubItem:
    def __init__(self, lib: "_StubLibrary", iid: int):
        self._lib = lib
        self.id = iid

    def remove(self, delete: bool = False, with_album: bool = True) -> None:
        conn = self._lib._get_connection()
        conn.execute("DELETE FROM item_attributes WHERE entity_id = ?", (self.id,))
        conn.execute("DELETE FROM items WHERE id = ?", (self.id,))
        conn.commit()


class _StubAlbum:
    def __init__(self, lib: "_StubLibrary", aid: int):
        self._lib = lib
        self.id = aid

    def items(self) -> list[_StubItem]:
        conn = self._lib._get_connection()
        rows = conn.execute("SELECT id FROM items WHERE album_id = ?", (self.id,)).fetchall()
        return [_StubItem(self._lib, r[0]) for r in rows]

    def remove(self, delete: bool = False, with_items: bool = True) -> None:
        self._lib.album_remove_calls.append((self.id, delete, with_items))
        conn = self._lib._get_connection()
        if with_items:
            conn.execute(
                "DELETE FROM item_attributes WHERE entity_id IN (SELECT id FROM items WHERE album_id = ?)",
                (self.id,),
            )
            conn.execute("DELETE FROM items WHERE album_id = ?", (self.id,))
        conn.execute("DELETE FROM album_attributes WHERE entity_id = ?", (self.id,))
        conn.execute("DELETE FROM albums WHERE id = ?", (self.id,))
        conn.commit()


class _StubLibrary:
    """Minimal beets.library.Library double backed by SQLite."""

    def __init__(self, path: str):
        self.path = path
        self.timeout = 5.0
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self.album_remove_calls: list[tuple[int, bool, bool]] = []

    def _get_connection(self) -> sqlite3.Connection:
        return self._conn

    def get_item(self, iid: int) -> _StubItem | None:
        row = self._conn.execute("SELECT 1 FROM items WHERE id = ?", (iid,)).fetchone()
        return _StubItem(self, iid) if row else None

    def get_album(self, aid: int) -> _StubAlbum | None:
        row = self._conn.execute("SELECT 1 FROM albums WHERE id = ?", (aid,)).fetchone()
        return _StubAlbum(self, aid) if row else None

    def close(self) -> None:
        self._conn.close()


class BlocklistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bl_path = Path(self.tmp.name) / "artist_blocklist.txt"
        self.orig_path = blocklist.PATH
        blocklist.PATH = str(self.bl_path)
        blocklist._reset_cache()
        self.addCleanup(self._restore_blocklist)

    def _restore_blocklist(self) -> None:
        blocklist.PATH = self.orig_path
        blocklist._reset_cache()

    def test_norm_edge_cases(self) -> None:
        self.assertEqual(blocklist.norm("The The"), "thethe")
        self.assertEqual(blocklist.norm("The"), "the")
        self.assertEqual(blocklist.norm("The Beatles"), "beatles")
        self.assertEqual(blocklist.norm("beatles"), "beatles")
        self.assertEqual(blocklist.norm("Große Straße"), "grossestrasse")
        self.assertEqual(blocklist.norm("STRAẞE"), "strasse")
        self.assertEqual(blocklist.norm("André Kostelanetz"), "andrekostelanetz")
        self.assertEqual(blocklist.norm("Artist feat. Someone"), "artist")
        self.assertEqual(blocklist.norm("Artist ft. Someone"), "artist")
        self.assertEqual(blocklist.norm("Artist featuring Someone"), "artist")
        self.assertEqual(blocklist.norm("Drake (feat. Rihanna)"), "drake")
        self.assertEqual(blocklist.norm("Artist [ft. Guest]"), "artist")
        self.assertEqual(blocklist.norm("Artist feat.Guest"), "artist")
        self.assertEqual(blocklist.norm("Artist Feather"), "artistfeather")
        self.assertEqual(blocklist.norm("Two Chainz"), "2chainz")
        self.assertEqual(blocklist.norm("2 Chainz"), "2chainz")
        self.assertEqual(blocklist.norm("-M-"), "m")
        self.assertEqual(blocklist.norm("北京"), "北京")
        self.assertEqual(blocklist.norm(""), "")
        self.assertEqual(blocklist.norm(None), "")
        self.assertEqual(blocklist.norm("!!!"), "")

    def test_invalid_utf8_read_does_not_crash(self) -> None:
        self.bl_path.write_bytes(b"# header\nBad\xffByte\nThe The\n")
        blocklist._reset_cache()
        entries = blocklist.entries()
        self.assertEqual(len(entries), 2)
        self.assertTrue(blocklist.is_blocked("The The"))
        self.assertTrue(blocklist.is_blocked("The\tThe"))

    def test_atomic_write_and_concurrent_add_entries(self) -> None:
        # Concurrent threads using add_entries must never lose updates or leave temp files
        names = [f"Artist {i:02d}" for i in range(40)]
        errors: list[Exception] = []

        def worker(batch: list[str]) -> None:
            try:
                for n in batch:
                    blocklist.add_entries([n])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(names[i::4],)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        got = blocklist.entries()
        self.assertEqual(len(got), 40)
        for n in names:
            self.assertTrue(blocklist.is_blocked(n))
        leftovers = [p.name for p in self.bl_path.parent.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])
        self.assertTrue((Path(str(self.bl_path) + ".lock")).exists())

    def test_concurrent_write_entries_no_corruption_or_race(self) -> None:
        errors: list[Exception] = []

        def writer(tag: int) -> None:
            try:
                for step in range(10):
                    blocklist.write_entries([f"Writer {tag} Item {step}", "Shared"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        entries = blocklist.entries()
        self.assertIn("Shared", entries)
        self.assertEqual(os.stat(self.bl_path).st_mode & 0o777, 0o644)
        leftovers = [p.name for p in self.bl_path.parent.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])


class BlocklistCtlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bl_path = Path(self.tmp.name) / "artist_blocklist.txt"
        self.orig_path = blocklist.PATH
        blocklist.PATH = str(self.bl_path)
        blocklist._reset_cache()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        blocklist.PATH = self.orig_path
        blocklist._reset_cache()

    def _run_ctl(self, argv: list[str]) -> tuple[int, dict]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = blocklist_ctl.main(argv)
        return rc, json.loads(buf.getvalue().strip())

    def test_existing_and_dash_prefixed_artist_invocations(self) -> None:
        rc, res = self._run_ctl(["add", "Normal Artist", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(res["added"], ["Normal Artist"])

        rc, res = self._run_ctl(["--json", "add", "--", "-M-"])
        self.assertEqual(rc, 0)
        self.assertEqual(res["added"], ["-M-"])
        self.assertEqual(res["count"], 2)

        rc, res = self._run_ctl(["--json", "check", "--", "-M-"])
        self.assertEqual(rc, 0)
        self.assertTrue(res["blocked"])

        rc, res = self._run_ctl(["list", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(set(res["entries"]), {"-M-", "Normal Artist"})

        rc, res = self._run_ctl(["remove", "--json", "--", "-M-", "NotPresent"])
        self.assertEqual(rc, 0)
        self.assertEqual(res["removed"], ["-M-"])
        self.assertEqual(res["missing"], ["NotPresent"])
        self.assertEqual(res["count"], 1)

    def test_subprocess_cli_with_double_dash(self) -> None:
        script = BEETS_AUTO / "jobs" / "blocklist_ctl.py"
        # Invoke via python -c setting blocklist.PATH so live repo file is untouched
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from lib import blocklist; blocklist.PATH = %r; "
            "from jobs import blocklist_ctl; "
            "sys.exit(blocklist_ctl.main(sys.argv[1:]))"
        ) % (str(BEETS_AUTO), str(self.bl_path))
        proc = subprocess.run(
            [sys.executable, "-c", code, "--json", "add", "--", "-M-"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout.strip())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["added"], ["-M-"])


class LibraryRemoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "musiclibrary.db"
        self.music_root = self.root / "merged" / "Music"
        self.quarantine = self.root / "quarantine"
        self.prune_root = self.quarantine / "library_prune"
        self.bl_path = self.root / "artist_blocklist.txt"
        self.music_root.mkdir(parents=True)
        self.quarantine.mkdir(parents=True)
        _make_beets_db(self.db_path)

        self.libs_created: list[_StubLibrary] = []
        beets_pkg = types.ModuleType("beets")
        beets_lib_mod = types.ModuleType("beets.library")

        def make_lib(path: str) -> _StubLibrary:
            lib = _StubLibrary(path)
            self.libs_created.append(lib)
            return lib

        beets_lib_mod.Library = make_lib
        beets_pkg.library = beets_lib_mod
        self.addCleanup(self._close_libs)

        self.patches = [
            mock.patch.dict(sys.modules, {"beets": beets_pkg, "beets.library": beets_lib_mod}),
            mock.patch.object(library_remove, "DB", str(self.db_path)),
            mock.patch.object(library_remove, "MUSIC_ROOT", str(self.music_root)),
            mock.patch.object(library_remove, "QUARANTINE", str(self.quarantine)),
            mock.patch.object(library_remove, "PRUNE_ROOT", str(self.prune_root)),
            mock.patch.object(
                library_remove,
                "storage_guard",
                return_value={"ok": True, "error": None, "quarantine": {}, "library": {}},
            ),
            mock.patch.object(
                library_remove,
                "_sync_media_servers",
                return_value={"navidrome": True},
            ),
            mock.patch.object(blocklist, "PATH", str(self.bl_path)),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        blocklist._reset_cache()
        self.addCleanup(blocklist._reset_cache)

    def _close_libs(self) -> None:
        for lib in self.libs_created:
            try:
                lib.close()
            except Exception:  # noqa: BLE001
                pass

    def _seed_album(
        self,
        album_id: int,
        artist: str,
        album: str,
        tracks: list[tuple[int, str]],
        *,
        with_art: bool = True,
    ) -> dict:
        album_dir = self.music_root / artist / album
        album_dir.mkdir(parents=True, exist_ok=True)
        art_path = album_dir / "cover.jpg"
        if with_art:
            art_path.write_bytes(b"artdata")
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO albums (id, albumartist, album, year, artpath) VALUES (?, ?, ?, ?, ?)",
                (album_id, artist, album, 2020, str(art_path).encode() if with_art else b""),
            )
            con.execute(
                "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                (album_id, "genre", "Electronic"),
            )
            paths: dict[int, Path] = {}
            for idx, (iid, filename) in enumerate(tracks, 1):
                p = album_dir / filename
                p.write_bytes(b"audio-" + filename.encode())
                paths[iid] = p
                con.execute(
                    "INSERT INTO items (id, album_id, albumartist, artist, album, title, disc, track, year, path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (iid, album_id, artist, artist, album, f"Track {idx}", 1, idx, 2020, str(p).encode()),
                )
                con.execute(
                    "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                    (iid, "mood", "chill"),
                )
            con.commit()
        finally:
            con.close()
        return {"album_dir": album_dir, "art_path": art_path, "paths": paths}

    def _run_main(self, argv: list[str]) -> tuple[int, dict]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = library_remove.main(argv)
        result_line = [ln for ln in buf.getvalue().splitlines() if ln.startswith("RESULT ")]
        payload = json.loads(result_line[-1][7:]) if result_line else {}
        return rc, payload

    def test_open_beets_library_sets_60s_busy_timeout(self) -> None:
        lib = library_remove._open_beets_library(str(self.db_path))
        busy = lib._get_connection().execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(busy, 60000)

        # Also test when Library accepts timeout= kwarg directly
        received: dict = {}

        class _TimeoutLib(_StubLibrary):
            def __init__(self, path: str, timeout: float = 5.0):
                super().__init__(path)
                received["timeout"] = timeout

        with mock.patch.object(sys.modules["beets.library"], "Library", _TimeoutLib):
            lib2 = library_remove._open_beets_library(str(self.db_path))
            lib2.close()
        self.assertEqual(received["timeout"], 60.0)

    def test_json_plan_and_dry_run_and_undo_dry_run(self) -> None:
        self._seed_album(5, "Plan Artist", "Plan Album", [(50, "01.flac")])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = library_remove.main(["--album-id", "5", "--json"])
        self.assertEqual(rc, 0)
        plan = json.loads(buf.getvalue().strip())
        self.assertEqual(plan["tracks"], 1)
        self.assertEqual(plan["album_rows"], 1)

        rc_dry, res_dry = self._run_main(["--album-id", "5"])
        self.assertEqual(rc_dry, 0)
        self.assertTrue(res_dry["dry_run"])

        rc_apply, res_apply = self._run_main(["--album-id", "5", "--apply"])
        self.assertEqual(rc_apply, 0)

        rc_undo_dry, res_undo_dry = self._run_main(["--undo", res_apply["manifest"]])
        self.assertEqual(rc_undo_dry, 0)
        self.assertTrue(res_undo_dry["dry_run"])
        self.assertEqual(res_undo_dry["files"], 1)

    def test_job_id_embedded_run_id_random_and_dash_artist_args(self) -> None:
        self._seed_album(1, "-M-", "Qui de nous deux", [(10, "01.flac")])
        self._seed_album(2, "-M-", "Je dis aime", [(20, "01.flac")])

        rc1, res1 = self._run_main(
            ["--album-id", "1", "--apply", "--job-id", "job-abc", "--reason=-bad-press-"]
        )
        self.assertEqual(rc1, 0)
        rc2, res2 = self._run_main(["--artist=-M-", "--apply", "--job-id", "job-def"])
        self.assertEqual(rc2, 0)

        m1 = json.loads(Path(res1["manifest"]).read_text(encoding="utf-8"))
        m2 = json.loads(Path(res2["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(m1["job_id"], "job-abc")
        self.assertEqual(m1["reason"], "-bad-press-")
        self.assertEqual(m2["job_id"], "job-def")
        self.assertEqual(m2["targets"]["artists"], ["-M-"])
        self.assertRegex(m1["id"], r"^\d{8}_\d{6}_[0-9a-f]{4}_[a-z0-9-]+$")
        self.assertRegex(m2["id"], r"^\d{8}_\d{6}_[0-9a-f]{4}_[a-z0-9-]+$")
        self.assertNotEqual(res1["run_dir"], res2["run_dir"])

    def test_album_row_not_removed_if_any_item_fails_to_move(self) -> None:
        seeded = self._seed_album(10, "Artist X", "Album X", [(101, "01.flac"), (102, "02.flac")])
        orig_move = shutil.move

        def failing_move(src, dst, *a, **kw):
            if str(src).endswith("02.flac"):
                raise PermissionError("simulated move failure")
            return orig_move(src, dst, *a, **kw)

        with mock.patch.object(library_remove.shutil, "move", side_effect=failing_move):
            rc, res = self._run_main(["--album-id", "10", "--apply"])

        self.assertEqual(rc, 2)
        self.assertEqual(res["moved"], 1)
        self.assertEqual(res["rows_removed"], 1)
        self.assertEqual(res["albums_removed"], 0)
        self.assertEqual(self.libs_created[-1].album_remove_calls, [])

        # Track 1 moved & row removed; track 2 & album row & cover art stay intact
        self.assertFalse(seeded["paths"][101].exists())
        self.assertTrue(seeded["paths"][102].exists())
        self.assertTrue(seeded["art_path"].exists())

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT id FROM items").fetchall(), [(102,)])
            self.assertEqual(con.execute("SELECT id FROM albums").fetchall(), [(10,)])
        finally:
            con.close()

        manifest = json.loads(Path(res["manifest"]).read_text(encoding="utf-8"))
        self.assertFalse(manifest["albums"][0]["row_removed"])

    def test_block_never_auto_blocks_various_artists_or_empty(self) -> None:
        self._seed_album(20, "Various Artists", "Compilation 1", [(201, "01.flac")])
        self._seed_album(21, "", "Tagless Album", [(202, "01.flac")])
        self._seed_album(22, "Real Blocked Artist", "Real Album", [(203, "01.flac")])

        rc1, res1 = self._run_main(["--album-id", "20", "--album-id", "21", "--apply", "--block"])
        self.assertEqual(rc1, 0)
        self.assertEqual(res1["blocked"], [])
        self.assertFalse(blocklist.is_blocked("Various Artists"))

        rc2, res2 = self._run_main(["--album-id", "22", "--apply", "--block"])
        self.assertEqual(rc2, 0)
        self.assertEqual(res2["blocked"], ["Real Blocked Artist"])
        self.assertTrue(blocklist.is_blocked("Real Blocked Artist"))

    def test_undo_handles_moved_true_row_removed_false_and_unremoved_album(self) -> None:
        seeded = self._seed_album(30, "Artist U", "Album U", [(301, "01.flac"), (302, "02.flac")])
        q_dir = self.prune_root / "20261122_010101_abcd_artist-u" / "Artist U" / "Album U"
        q_dir.mkdir(parents=True)
        q_file = q_dir / "01.flac"
        shutil.move(seeded["paths"][301], q_file)

        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        try:
            album_row = library_remove.enc_row(con.execute("SELECT * FROM albums WHERE id=30").fetchone())
            item1_row = library_remove.enc_row(con.execute("SELECT * FROM items WHERE id=301").fetchone())
            item2_row = library_remove.enc_row(con.execute("SELECT * FROM items WHERE id=302").fetchone())
            # Track 2's row WAS removed (and file moved); track 1's row was NOT removed!
            q_file2 = q_dir / "02.flac"
            shutil.move(seeded["paths"][302], q_file2)
            con.execute("DELETE FROM items WHERE id = 302")
            con.commit()
        finally:
            con.close()

        manifest_path = self.prune_root / "20261122_010101_abcd_artist-u" / "manifest.json"
        manifest = {
            "id": "20261122_010101_abcd_artist-u",
            "version": 1,
            "ts": 1700000000,
            "requested_by": "test",
            "reason": "",
            "targets": {"artists": [], "album_ids": [30], "item_ids": []},
            "block": False,
            "plan": {"artists": ["Artist U"], "bytes": 0},
            "albums": [
                {
                    "album_id": 30,
                    "row": album_row,
                    "artpath": str(seeded["art_path"]),
                    "art_dst": "",
                    "row_removed": False,
                }
            ],
            "items": [
                {
                    "item_id": 301,
                    "album_id": 30,
                    "src": str(seeded["paths"][301]),
                    "dst": str(q_file),
                    "row": item1_row,
                    "moved": True,
                    "row_removed": False,
                },
                {
                    "item_id": 302,
                    "album_id": 30,
                    "src": str(seeded["paths"][302]),
                    "dst": str(q_file2),
                    "row": item2_row,
                    "moved": True,
                    "row_removed": True,
                },
            ],
            "extras": [],
            "errors": [],
            "item_attributes": [],
            "album_attributes": [
                {"entity_id": 30, "key": "genre", "value": "Electronic"}
            ],
            "undone_ts": None,
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        rc, res = self._run_main(["--undo", str(manifest_path), "--apply"])
        self.assertEqual(rc, 0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["files"], 2)
        self.assertEqual(res["rows"], 1)       # only 302 re-inserted; 301 was already in DB
        self.assertEqual(res["albums"], 0)     # album 30 had row_removed=False, not duplicated
        self.assertTrue(seeded["paths"][301].exists())
        self.assertTrue(seeded["paths"][302].exists())

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM albums").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM album_attributes").fetchone()[0], 1)
            self.assertEqual(
                [r[0] for r in con.execute("SELECT id FROM items ORDER BY id").fetchall()],
                [301, 302],
            )
        finally:
            con.close()

    def test_full_apply_then_undo_and_db_error_resilience(self) -> None:
        seeded = self._seed_album(40, "Artist R", "Album R", [(401, "01.flac"), (402, "02.flac")])
        extra = seeded["album_dir"] / "notes.txt"
        extra.write_text("liner notes", encoding="utf-8")

        rc, res = self._run_main(["--album-id", "40", "--apply", "--block", "--job-id", "j-99"])
        self.assertEqual(rc, 0)
        self.assertEqual(res["moved"], 2)
        self.assertEqual(res["albums_removed"], 1)
        self.assertEqual(res["extras"], 1)
        self.assertTrue(blocklist.is_blocked("Artist R"))

        manifest_path = Path(res["manifest"])
        # Simulate a DB insert error on item 402 during first undo attempt
        real_connect = sqlite3.connect
        fail_state = {"fail_402": True}

        class _FlakyConn:
            def __init__(self, inner: sqlite3.Connection):
                self._inner = inner

            def __getattr__(self, name: str):
                return getattr(self._inner, name)

            def execute(self, sql: str, params=()):
                if fail_state["fail_402"] and "INSERT INTO items" in sql and 402 in params:
                    raise sqlite3.OperationalError("simulated database is locked")
                return self._inner.execute(sql, params)

        def flaky_connect(*a, **kw):
            return _FlakyConn(real_connect(*a, **kw))

        with mock.patch.object(library_remove.sqlite3, "connect", side_effect=flaky_connect):
            rc_fail, res_fail = self._run_main(["--undo", str(manifest_path), "--apply"])

        self.assertEqual(rc_fail, 1)
        self.assertFalse(res_fail["ok"])
        m_mid = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIsNone(m_mid["undone_ts"])
        self.assertTrue(blocklist.is_blocked("Artist R"))  # not unblocked on failed undo

        # Retry without error: succeeds and does NOT duplicate album 40 or item 401
        rc_ok, res_ok = self._run_main(["--undo", str(manifest_path), "--apply"])
        self.assertEqual(rc_ok, 0)
        self.assertTrue(res_ok["ok"])
        self.assertFalse(blocklist.is_blocked("Artist R"))
        self.assertTrue(seeded["paths"][401].exists())
        self.assertTrue(seeded["paths"][402].exists())
        self.assertTrue(seeded["art_path"].exists())
        self.assertTrue(extra.exists())

        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM albums").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM album_attributes").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM item_attributes").fetchone()[0], 2)
        finally:
            con.close()

        # Re-running undo after completion refuses cleanly
        rc_again, res_again = self._run_main(["--undo", str(manifest_path), "--apply"])
        self.assertEqual(rc_again, 1)
        self.assertEqual(res_again["error"], "already undone")

    def test_concurrently_imported_item_prevents_album_row_removal(self) -> None:
        seeded = self._seed_album(50, "Artist C", "Album C", [(501, "01.flac")])
        orig_get_album = _StubLibrary.get_album

        def inject_concurrent_item(lib_self, aid: int):
            if aid == 50:
                conn = lib_self._get_connection()
                exists = conn.execute("SELECT 1 FROM items WHERE id = 599").fetchone()
                if not exists:
                    late_path = seeded["album_dir"] / "02_late.flac"
                    late_path.write_bytes(b"late-audio")
                    conn.execute(
                        "INSERT INTO items (id, album_id, albumartist, artist, album, title, disc, track, year, path) "
                        "VALUES (599, 50, 'Artist C', 'Artist C', 'Album C', 'Late Track', 1, 2, 2020, ?)",
                        (str(late_path).encode(),),
                    )
                    conn.commit()
            return orig_get_album(lib_self, aid)

        with mock.patch.object(_StubLibrary, "get_album", inject_concurrent_item):
            rc, res = self._run_main(["--album-id", "50", "--apply"])

        self.assertEqual(rc, 0)
        self.assertEqual(res["albums_removed"], 0)
        self.assertTrue(seeded["art_path"].exists())
        con = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(con.execute("SELECT id FROM albums").fetchall(), [(50,)])
            self.assertEqual(con.execute("SELECT id FROM items").fetchall(), [(599,)])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()

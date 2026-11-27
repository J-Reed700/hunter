"""Regression tests for architecture, security, SQL, pipeline, and web hardening."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hunter import (
    auth,
    beetcmd,
    beetsops,
    blocklist,
    config,
    db,
    downloads,
    jobs,
    libops,
    libquery,
    library,
    notify,
    pipeline,
    recovery,
    state,
)
from hunter.web import common, start_ui
from hunter.web.routes import beets as beets_routes
from hunter.web.routes import library as library_routes


def _cfg(**overrides) -> config.HunterConfig:
    with mock.patch.dict(os.environ, {}, clear=True):
        base = config.HunterConfig.from_env()
    if "state_dir" in overrides and "state_db" not in overrides:
        overrides["state_db"] = Path(overrides["state_dir"]) / "hunter.db"
    return dataclasses.replace(base, **overrides)


def _http(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = dict(headers or {})
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def _make_beets_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
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
            album TEXT,
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
            bpm INTEGER,
            genre TEXT,
            year INTEGER,
            original_year INTEGER,
            mb_trackid TEXT,
            mb_releasetrackid TEXT,
            mb_albumid TEXT,
            mb_artistid TEXT,
            mb_albumartistid TEXT,
            mb_releasegroupid TEXT,
            acoustid_fingerprint TEXT,
            lyrics TEXT,
            comments TEXT,
            comp INTEGER,
            albumtype TEXT,
            label TEXT,
            catalognum TEXT,
            country TEXT,
            path BLOB,
            mtime REAL,
            added REAL
        );
        CREATE TABLE item_attributes (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER,
            key TEXT,
            value TEXT
        );
        CREATE TABLE album_attributes (
            id INTEGER PRIMARY KEY,
            entity_id INTEGER,
            key TEXT,
            value TEXT
        );
        """
    )
    conn.commit()
    return conn


class Layer0And1Regressions(unittest.TestCase):
    def test_notify_detect_kind_does_not_misclassify_ntfy_messages_topic(self) -> None:
        self.assertEqual(notify._detect_kind("https://ntfy.sh/messages"), "ntfy")
        self.assertEqual(notify._detect_kind("https://ntfy.example.com/hunter_messages"), "ntfy")
        self.assertEqual(notify._detect_kind("https://gotify.example.com/message"), "gotify")
        self.assertEqual(notify._detect_kind("https://gotify.example.com/message?token=abc"), "gotify")
        self.assertEqual(
            notify._detect_kind("https://discord.com/api/webhooks/123/secret"),
            "discord",
        )

    def test_auth_role_order_and_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "hunter.db"
            a = auth.Auth(db_path)
            try:
                a.create_first_admin("z_admin", "password123")
                a.create_user("a_viewer", "password123", role="viewer", created_by="z_admin")
                a.create_user("m_op", "password123", role="operator", created_by="z_admin")
                users = a.users()
                self.assertEqual([u["role"] for u in users], ["admin", "operator", "viewer"])
            finally:
                a.close()

        self.assertFalse(auth._should_redact("exit_code"))
        self.assertFalse(auth._should_redact("country_code"))
        self.assertTrue(auth._should_redact("password"))
        self.assertTrue(auth._should_redact("sp_dc"))
        self.assertTrue(auth._should_redact("access_token"))
        self.assertTrue(auth._should_redact("client_secret"))


class BeetcmdRegressions(unittest.TestCase):
    def test_flag_value_does_not_bypass_dry_run_or_confirm_injection(self) -> None:
        cfg = _cfg(beets_cli="beet -c /custom.yaml")
        self.assertEqual(beetcmd.beet_argv(cfg), ["beet", "-c", "/custom.yaml"])

        # Valued flag whose value looks like `-p` or `-y` must not fool _present_flags().
        fake_cmd = beetcmd.Cmd(
            name="move",
            title="Move",
            cls="mutate",
            blurb="test",
            flags=(beetcmd.Flag("-F", "Format", arg="fmt"),),
            confirm=("-y",),
            pretend=("-p",),
        )
        argv_dry = ["move", "-F", "-p", "album:foo"]
        beetcmd._apply_mode_flags(fake_cmd, argv_dry, apply=False)
        self.assertEqual(argv_dry.count("-p"), 2)

        argv_apply = ["move", "-F", "-y", "album:foo"]
        beetcmd._apply_mode_flags(fake_cmd, argv_apply, apply=True)
        self.assertEqual(argv_apply.count("-y"), 2)

        # Nested allowed root must reject a sibling directly at root depth.
        self.assertFalse(beetcmd._is_confined_container_path("/a/b", allowed_roots=("/a/b",)))
        self.assertTrue(beetcmd._is_confined_container_path("/a/b/file.jpg", allowed_roots=("/a/b",)))
        self.assertFalse(beetcmd._is_confined_container_path("/a/b/../secret.jpg", allowed_roots=("/a/b",)))


class LibqueryRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.beets_db = self.root / "beets.db"
        self.cfg = _cfg(
            beets_db=self.beets_db,
            music_root=self.music,
            state_dir=self.root / "state",
        )
        now = time.time()
        conn = _make_beets_db(self.beets_db)
        try:
            conn.execute(
                "INSERT INTO albums(id, albumartist, album, year, genre, albumtype, added) VALUES(1, 'EmptyArt', 'Ghost', 2020, 'Rock', 'album', ?)",
                (now,),
            )
            conn.execute(
                "INSERT INTO albums(id, albumartist, album, year, genre, albumtype, mb_albumartistid, added) VALUES(2, 'HiResArt', 'Crystal', 2023, 'Jazz', 'album', 'mb-2', ?)",
                (now - 3600,),
            )
            conn.execute(
                "INSERT INTO items(id, album_id, artist, album, albumartist, title, disc, disctotal, track, tracktotal, length, bitrate, format, samplerate, bitdepth, genre, year, path, added) "
                "VALUES(10, 2, 'HiResArt', 'Crystal', 'HiResArt', 'Track 1', 1, 1, 1, 1, 200, 1411000, 'FLAC', 96000, 24, 'Jazz', 2023, ?, ?)",
                (b"/music/hires/01.flac", now - 3600),
            )
            conn.execute(
                "INSERT INTO albums(id, albumartist, album, year, genre, albumtype, added) VALUES(3, 'DoubleArt', 'TwoDiscs', 2021, 'Rock', 'album', ?)",
                (now - 20 * 86400,),
            )
            for t in (1, 2):
                conn.execute(
                    "INSERT INTO items(id, album_id, artist, album, albumartist, title, disc, disctotal, track, tracktotal, length, bitrate, format, samplerate, bitdepth, genre, year, path, added) "
                    "VALUES(?, 3, 'DoubleArt', 'TwoDiscs', 'DoubleArt', ?, 1, 2, ?, 2, 180, 320000, 'MP3', 44100, 16, 'Rock', 2021, ?, ?)",
                    (20 + t, f"T{t}", t, f"/music/dbl/0{t}.mp3".encode(), now - 20 * 86400),
                )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_empty_album_excluded_from_lossy_and_complete(self) -> None:
        lossy = libquery.albums(self.cfg, filt="lossy")["albums"]
        complete = libquery.albums(self.cfg, filt="complete")["albums"]
        self.assertNotIn(1, [a["id"] for a in lossy])
        self.assertNotIn(1, [a["id"] for a in complete])

    def test_hires_precedence_with_combined_filter(self) -> None:
        res = libquery.albums(self.cfg, filt="hires", genre="Rock")
        self.assertEqual(res["total"], 0)
        res_ok = libquery.albums(self.cfg, filt="hires", genre="Jazz")
        self.assertEqual([a["id"] for a in res_ok["albums"]], [2])

        tr_none = libquery.tracks(self.cfg, filt="hires", genre="Rock")
        self.assertEqual(tr_none["total"], 0)
        tr_ok = libquery.tracks(self.cfg, filt="hires", genre="Jazz")
        self.assertEqual([t["id"] for t in tr_ok["tracks"]], [10])

    def test_max_tracks_zero_matches_empty_albums(self) -> None:
        res = libquery.albums(self.cfg, max_tracks=0)
        self.assertEqual([a["id"] for a in res["albums"]], [1])

    def test_multidisc_missing_whole_disc_is_incomplete(self) -> None:
        inc = libquery.albums(self.cfg, filt="incomplete")["albums"]
        by_id = {a["id"]: a for a in inc}
        self.assertIn(3, by_id)
        self.assertEqual(by_id[3]["tracks"], 2)
        self.assertGreaterEqual(by_id[3]["tracktotal"], 4)

    def test_artists_noartistid_totals_and_pulse_counts_added_7d(self) -> None:
        res = libquery.artists(self.cfg, filt="noartistid")
        names = {a["name"] for a in res["artists"]}
        self.assertIn("DoubleArt", names)
        self.assertNotIn("HiResArt", names)
        self.assertEqual(res["total"], len(res["artists"]))

        pulse = libquery.pulse_counts(self.cfg)
        self.assertIn("added_7d", pulse)
        self.assertGreaterEqual(pulse["added_7d"], 1)


class PipelineAndRecoveryRegressions(unittest.TestCase):
    def test_recovery_files_present_rejects_partial_loss_album(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            music = root / "music"
            music.mkdir()
            db_path = root / "beets.db"
            cfg = _cfg(
                beets_db=db_path,
                music_root=music,
                beets_music_root="/music",
                owned_coverage=0.8,
            )
            conn = _make_beets_db(db_path)
            try:
                conn.execute("INSERT INTO albums(id, albumartist, album) VALUES(1, 'A', 'B')")
                for i in range(1, 11):
                    rel = f"A/B/{i:02d}.flac"
                    if i <= 8:
                        p = music / rel
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(b"fLaC")
                    conn.execute(
                        "INSERT INTO items(id, album_id, title, path) VALUES(?, 1, ?, ?)",
                        (i, f"T{i}", f"/music/{rel}".encode()),
                    )
                conn.commit()
            finally:
                conn.close()

            self.assertFalse(pipeline._recovery_files_present(cfg, 1, 10))
            for i in (9, 10):
                (music / f"A/B/{i:02d}.flac").write_bytes(b"fLaC")
            self.assertTrue(pipeline._recovery_files_present(cfg, 1, 10))

    def test_recovery_scan_ignores_unmapped_container_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            music = root / "music"
            music.mkdir()
            db_path = root / "beets.db"
            cfg = _cfg(
                beets_db=db_path,
                music_root=music,
                beets_music_root="/music",
                state_dir=root / "state",
                storage_fstype="",
            )
            conn = _make_beets_db(db_path)
            try:
                (music / "inside").mkdir(parents=True, exist_ok=True)
                (music / "inside" / "01.flac").write_bytes(b"fLaC")
                conn.execute("INSERT INTO albums(id, albumartist, album, year) VALUES(1, 'Outside', 'Album', 2020)")
                conn.execute(
                    "INSERT INTO items(id, album_id, artist, album, albumartist, title, path) "
                    "VALUES(1, 1, 'Outside', 'Album', 'Outside', 'T1', ?)",
                    (b"/other_mount/outside/01.flac",),
                )
                conn.execute("INSERT INTO albums(id, albumartist, album, year) VALUES(2, 'Inside', 'Present', 2020)")
                conn.execute(
                    "INSERT INTO items(id, album_id, artist, album, albumartist, title, path) "
                    "VALUES(2, 2, 'Inside', 'Present', 'Inside', 'T1', ?)",
                    (b"/music/inside/01.flac",),
                )
                conn.commit()
            finally:
                conn.close()

            recovery._CACHE.clear()
            snap = recovery.scan(cfg)
            self.assertTrue(snap["trusted"], snap)
            self.assertEqual(snap["unmapped"], 1)
            self.assertEqual(snap["missing"], 0)
            self.assertEqual(snap["tracks"], [])

    def test_watch_artists_deadline_does_not_mark_artist_checked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = _cfg(state_dir=root / "state")
            st = state.HunterState(cfg.state_db)
            try:
                st.follow_artist("mb-1", "Artist One")
                fake_lib = mock.MagicMock()
                fake_lib.lookup_owned.return_value = (False, 0, 0)
                with mock.patch("hunter.mb.artist_release_groups") as m_mb:
                    with mock.patch.object(pipeline.time, "monotonic", side_effect=[100.0, 101.0]):
                        pipeline.watch_artists(cfg, st, fake_lib, deadline=0.1)
                    m_mb.assert_not_called()
                row = st.followed_artists()[0]
                self.assertFalse(row["last_check_ts"])
            finally:
                st.close()


class JobsAndLibopsRegressions(unittest.TestCase):
    def test_stale_starting_sweep_and_finish_removal_refuses_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = _cfg(state_dir=root / "state")
            called: list[str] = []
            jm = jobs.Jobs(cfg, finalizers={"library": lambda _jm, job: called.append(job["id"])})
            old_ts = int(time.time()) - 600
            conn = db.open_hunter_db(cfg.state_db)
            try:
                conn.execute(
                    "INSERT INTO jobs(id, kind, title, argv, mode, runner, log, status, started_ts, meta) "
                    "VALUES('j_stale', 'library', 'remove', '[]', 'apply', 'beets', 'j_stale.log', 'starting', ?, ?)",
                    (old_ts, json.dumps({"op": "remove", "run_id": "r1"})),
                )
                conn.commit()
            finally:
                conn.close()
            jm.refresh()
            self.assertEqual(called, ["j_stale"])
            j = jm.get("j_stale")
            self.assertEqual(j["status"], "failed")

            st = state.HunterState(cfg.state_db)
            try:
                st.upsert_want("k1", "BlockMe", "Alb", 2020, 8, "manual")
                note = libops._park_note("j_fail")
                st.park_wants(["BlockMe"], [], note)
                with mock.patch.object(libops, "job_result", return_value={"ok": False, "error": "disk full"}):
                    libops._finish_removal(
                        cfg,
                        {"id": "j_fail", "status": "ok", "exit_code": 0},
                        {"op": "remove", "artists": ["BlockMe"], "block": True, "side": {"drop_wants": True, "artists": ["BlockMe"]}},
                    )
                w = st.get_want(1)
                self.assertEqual(w.status, "wanted")
            finally:
                st.close()

    def test_beets_wrapper_preserves_exit_code_not_and_and(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = _cfg(state_dir=root / "state")
            jm = jobs.Jobs(cfg)
            captured: list[list[str]] = []

            fake_docker = mock.MagicMock()
            fake_docker.available = True

            def fake_exec_run(_container, cmd, **_kw):
                captured.append(list(cmd))
                return {"exec_id": "exec-123"}

            fake_docker.exec_run.side_effect = fake_exec_run
            jm.docker = fake_docker
            res = jm.start_beets(kind="beet", title="t", argv=["beet", "stats"], mode="read")
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(captured), 1)
            shell_snippet = captured[0][-1]
            self.assertIn("; exit $rc", shell_snippet)
            self.assertNotIn("&& exit", shell_snippet)


class SpotifySyncRegressions(unittest.TestCase):
    def test_album_artist_persisted_and_used_in_playlist_album_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "hunter.db"
            st = state.HunterState(db_path)
            try:
                st.upsert_playlist("pl1", "Mix", 1)
                st.sp_upsert_track(
                    "t1",
                    "Song",
                    "Feat Artist",
                    "Rec",
                    "a1",
                    2022,
                    180,
                    10,
                    album_artist="Primary Artist",
                )
                st.sp_link_track("pl1", "t1", 0, 1)
                rows = st.sp_playlist_album_rows("pl1")
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["artist"], "Primary Artist")
            finally:
                st.close()


class WebSecurityAndRoutesRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.beets_db = self.root / "beets.db"
        self.dl_dir = self.root / "downloads"
        self.dl_dir.mkdir()
        self.docker_sock = self.root / "docker.sock"
        self.docker_sock.write_bytes(b"")

        conn = _make_beets_db(self.beets_db)
        try:
            f = self.music / "band" / "01.mp3"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"ID3")
            conn.execute(
                "INSERT INTO albums(id, albumartist, album, year, added) VALUES(1, 'Band', 'Rec', 2022, ?)",
                (time.time(),),
            )
            conn.execute(
                "INSERT INTO items(id, album_id, artist, album, albumartist, title, track, tracktotal, length, bitrate, format, path, added) "
                "VALUES(1, 1, 'Band', 'Rec', 'Band', 'One', 1, 1, 180, 320000, 'MP3', ?, ?)",
                (b"/music/band/01.mp3", time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "HUNTER_STATE_DIR": str(self.state_dir),
                "HUNTER_BEETS_DB": str(self.beets_db),
                "HUNTER_MUSIC_ROOT": str(self.music),
                "HUNTER_DL_DIR": str(self.dl_dir),
                "DOCKER_SOCK": str(self.docker_sock),
                "HUNTER_STORAGE_FSTYPE": "",
                "HUNTER_UI_TOKEN": "test-api-token-xyz",
                "BEETS_CLI": "beet -c /custom.yaml",
            },
            clear=False,
        )
        self._env_patch.start()
        self._reset_caches()

        self.cfg = config.HunterConfig.from_env()
        db.open_hunter_db(self.cfg.state_db).close()

        a = auth.Auth(self.cfg.state_db)
        try:
            admin_u = a.create_first_admin("admin1", "adminpass123")
            self.admin_id = int(admin_u["id"])
            a.create_user("op1", "oppass1234", "operator", created_by="admin1")
            a.create_user("view1", "viewpass123", "viewer", created_by="admin1")
            a.create_user("view2", "viewpass123", "viewer", created_by="admin1")
        finally:
            a.close()

        self._spawn_patch = mock.patch.object(common, "spawn_pass")
        self._spawn_patch.start()

        self.httpd = start_ui(self.cfg, host="127.0.0.1", port=0)
        assert self.httpd is not None
        self.port = self.httpd.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.admin_cookie = self._login("admin1", "adminpass123")
        self.op_cookie = self._login("op1", "oppass1234")
        self.v1_cookie = self._login("view1", "viewpass123")
        self.v2_cookie = self._login("view2", "viewpass123")

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._spawn_patch.stop()
        self._reset_caches()
        self._env_patch.stop()
        self._td.cleanup()

    def _reset_caches(self) -> None:
        db.reset_schema_cache()
        config.clear_config_cache()
        library.invalidate_library_cache()
        blocklist.invalidate()
        beetsops.cache.drop("watcher_config")
        with auth._fails_lock:
            auth._fails.clear()

    def _login(self, username: str, password: str) -> str:
        st, hdrs, _ = _http(
            self.port,
            "POST",
            "/api/auth/login",
            body={"username": username, "password": password},
            headers={"Origin": self.origin},
        )
        self.assertEqual(st, 200)
        return hdrs["set-cookie"].split(";", 1)[0]

    def test_query_token_restricted_and_method_not_allowed(self) -> None:
        st, _, _ = _http(self.port, "GET", f"/api/status?token={self.cfg.ui_token}")
        self.assertEqual(st, 401)
        st, _, _ = _http(self.port, "GET", f"/calendar.ics?token={self.cfg.ui_token}")
        self.assertEqual(st, 200)
        st, _, _ = _http(self.port, "POST", "/healthz")
        self.assertEqual(st, 405)

    def test_legacy_routes_removed(self) -> None:
        hdrs = {"Cookie": self.admin_cookie}
        for path in ("/api/mode", "/api/banned", "/api/playlists", "/api/library_artists"):
            st, _, _ = _http(self.port, "GET", path, headers=hdrs)
            self.assertEqual(st, 404, f"legacy route {path} should be 404")

    def test_downloads_viewer_idor_blocked_and_list_scoped(self) -> None:
        dl_dir = self.state_dir / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        b1_zip = dl_dir / "b1.zip"
        b2_zip = dl_dir / "b2.zip"
        b1_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        b2_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        now = int(time.time())
        downloads._save(
            self.cfg,
            [
                {
                    "id": "b1",
                    "title": "V1 Zip",
                    "filename": "v1.zip",
                    "status": "ready",
                    "files": 1,
                    "done": 1,
                    "bytes": 22,
                    "path": str(b1_zip),
                    "created_ts": now,
                    "expires_ts": now + 3600,
                    "requested_by": "view1",
                },
                {
                    "id": "b2",
                    "title": "V2 Zip",
                    "filename": "v2.zip",
                    "status": "ready",
                    "files": 1,
                    "done": 1,
                    "bytes": 22,
                    "path": str(b2_zip),
                    "created_ts": now,
                    "expires_ts": now + 3600,
                    "requested_by": "view2",
                },
            ],
        )

        st, _, raw = _http(self.port, "GET", "/api/downloads", headers={"Cookie": self.v1_cookie})
        self.assertEqual(st, 200)
        data = json.loads(raw)
        self.assertEqual([b["id"] for b in data["bundles"]], ["b1"])
        self.assertNotIn("path", data["bundles"][0])

        st, _, _ = _http(self.port, "GET", "/api/downloads/file?id=b2", headers={"Cookie": self.v1_cookie})
        self.assertEqual(st, 403)
        st, _, _ = _http(self.port, "GET", "/api/downloads/file?id=b1", headers={"Cookie": self.v1_cookie})
        self.assertEqual(st, 200)

    def test_admin_self_reset_blocked_on_users_password(self) -> None:
        st, _, raw = _http(
            self.port,
            "POST",
            "/api/users/password",
            headers={"Cookie": self.admin_cookie, "Origin": self.origin},
            body={"id": self.admin_id, "new": "newpassword123"},
        )
        self.assertEqual(st, 400)
        self.assertIn("/api/auth/password", raw.decode("utf-8").lower())

    def test_viewer_status_redacted_and_artist_refresh_gated(self) -> None:
        st, _, raw = _http(self.port, "GET", "/api/status", headers={"Cookie": self.v1_cookie})
        self.assertEqual(st, 200)
        body = json.loads(raw)
        self.assertEqual(body.get("jobs"), [])
        self.assertFalse(body.get("quarantine_dir"))

        with mock.patch("hunter.mb.artist_release_groups", return_value=[]) as m_mb:
            st, _, _ = _http(
                self.port,
                "GET",
                "/api/artist_albums?id=00000000-0000-0000-0000-000000000001&refresh=1",
                headers={"Cookie": self.v1_cookie},
            )
            self.assertEqual(st, 200)
            m_mb.assert_called_once()
            self.assertFalse(m_mb.call_args.kwargs.get("refresh"))

    def test_beets_splupdate_apply_not_blocked_by_409_and_library_beet_track_no_album_flag(self) -> None:
        fake_docker = mock.MagicMock()
        fake_docker.available = True
        fake_docker.exec_run.return_value = {"exit_code": 0, "output": "1\tBand - One\n__HUNTER_COUNT__=1\n"}
        fake_docker.exec_detached.return_value = {"ok": True, "exec_id": "exec-spl"}
        with mock.patch.object(beets_routes, "DockerClient", return_value=fake_docker):
            with mock.patch("hunter.web.common.jobs") as m_jobs:
                m_jobs.return_value.start_beets.return_value = {"ok": True, "job": {"id": "j_spl", "status": "running"}}
                st, _, raw = _http(
                    self.port,
                    "POST",
                    "/api/beets/run",
                    headers={"Cookie": self.op_cookie, "Origin": self.origin},
                    body={"command": "splupdate", "apply": True},
                )
                self.assertEqual(st, 200, raw.decode())

        captured_argv: list[list[str]] = []

        def fake_start_beets(*, argv, **_kw):
            captured_argv.append(list(argv))
            return {"ok": True, "job": {"id": "j_trk", "status": "running"}}

        with mock.patch.object(library_routes, "DockerClient", return_value=fake_docker):
            with mock.patch("hunter.web.common.jobs") as m_jobs:
                m_jobs.return_value.start_beets.side_effect = fake_start_beets
                st, _, raw = _http(
                    self.port,
                    "POST",
                    "/api/library/beet",
                    headers={"Cookie": self.op_cookie, "Origin": self.origin},
                    body={"action": "write", "item_id": 1},
                )
                self.assertEqual(st, 200, raw.decode())
                self.assertEqual(len(captured_argv), 1)
                self.assertEqual(captured_argv[0][:3], ["beet", "-c", "/custom.yaml"])
                self.assertNotIn("-a", captured_argv[0])


class BundledStandaloneHelpersRegressions(unittest.TestCase):
    def test_native_blocklist_fallback_works_without_docker_or_beets_automation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = _cfg(
                state_dir=root / "state",
                beets_automation_dir=root / "nonexistent_auto",
            )
            blocklist.invalidate()
            self.assertFalse(blocklist.uses_external(cfg))
            self.assertTrue(blocklist.available(cfg))
            self.assertEqual(blocklist.entries(cfg), [])

            no_docker = mock.MagicMock()
            no_docker.available = False
            res = libops.blocklist_edit(
                cfg, no_docker, add=["The Beatles", "beatles", "Radiohead"], remove=[]
            )
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["added"], ["The Beatles", "Radiohead"])
            self.assertEqual(res["skipped"], ["beatles"])
            self.assertEqual(res["count"], 2)
            self.assertTrue(blocklist.is_blocked("Beatles", cfg))
            self.assertTrue((root / "state" / "artist_blocklist.txt").is_file())

            res2 = libops.blocklist_edit(cfg, no_docker, add=[], remove=["beatles", "Nobody"])
            self.assertTrue(res2["ok"], res2)
            self.assertEqual(res2["removed"], ["beatles"])
            self.assertEqual(res2["missing"], ["Nobody"])
            self.assertEqual(res2["count"], 1)
            self.assertFalse(blocklist.is_blocked("The Beatles", cfg))
            self.assertTrue(blocklist.is_blocked("Radiohead", cfg))

    def test_bundled_library_remove_preflight_remove_and_undo_end_to_end(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            music = root / "music"
            quarantine = root / "quarantine"
            album_dir = music / "Band" / "Debut"
            album_dir.mkdir(parents=True)
            quarantine.mkdir(parents=True)
            track_path = album_dir / "01.flac"
            art_path = album_dir / "cover.jpg"
            nfo_path = album_dir / "info.nfo"
            track_path.write_bytes(b"fLaC-audio-data")
            art_path.write_bytes(b"\xff\xd8\xff-jpg")
            nfo_path.write_text("liner notes", encoding="utf-8")

            db_path = root / "musiclibrary.db"
            conn = _make_beets_db(db_path)
            try:
                conn.execute(
                    "INSERT INTO albums(id, albumartist, album, year, artpath, added) VALUES(1, 'Band', 'Debut', 2022, ?, ?)",
                    (str(art_path).encode("utf-8"), time.time()),
                )
                conn.execute(
                    "INSERT INTO album_attributes(entity_id, key, value) VALUES(1, 'mood', 'energetic')"
                )
                conn.execute(
                    "INSERT INTO items(id, album_id, artist, album, albumartist, title, track, tracktotal, length, bitrate, format, path, added) "
                    "VALUES(10, 1, 'Band', 'Debut', 'Band', 'Track One', 1, 1, 180, 900000, 'FLAC', ?, ?)",
                    (str(track_path).encode("utf-8"), time.time()),
                )
                conn.execute(
                    "INSERT INTO item_attributes(entity_id, key, value) VALUES(10, 'rating', '5')"
                )
                conn.commit()
            finally:
                conn.close()

            cfg = _cfg(
                state_dir=root / "state",
                beets_db=db_path,
                music_root=music,
                quarantine_dir=quarantine,
                beets_music_root=str(music),
                beets_quarantine_root=str(quarantine),
                beets_automation_dir=root / "nonexistent_auto",
                beets_python=sys.executable,
                storage_fstype="",
            )

            def run_bundled(extra_args: list[str]) -> tuple[int, dict]:
                argv, env, is_bundled = libops._removal_exec_spec(cfg, ["--db", str(db_path), *extra_args])
                self.assertTrue(is_bundled)
                proc = subprocess.run(
                    argv,
                    env={**os.environ, **(env or {})},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                parsed = libops._parse_result_json(proc.stdout)
                self.assertIsNotNone(parsed, f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
                return proc.returncode, parsed

            # 1. Preflight
            rc, pf = run_bundled(["--preflight"])
            self.assertEqual(rc, 0, pf)
            self.assertTrue(pf["ok"])
            self.assertGreater(pf["quarantine"]["free_bytes"], 0)

            # 2. Journaled removal
            rc, rem = run_bundled(["--artist=Band", "--apply", "--requested-by=admin1", "--job-id=j_test_1"])
            self.assertEqual(rc, 0, rem)
            self.assertTrue(rem["ok"])
            self.assertEqual(rem["moved"], 1)
            self.assertEqual(rem["albums_removed"], 1)
            self.assertFalse(track_path.exists())
            self.assertFalse(art_path.exists())
            self.assertFalse(nfo_path.exists())
            self.assertFalse(album_dir.exists())

            # Manifest readable by libops.removals()
            runs = libops.removals(cfg)
            self.assertEqual(len(runs), 1)
            run_id = runs[0]["id"]
            self.assertEqual(runs[0]["job_id"], "j_test_1")
            detail = libops.removal(cfg, run_id)
            self.assertIsNotNone(detail)

            # DB rows removed
            c = sqlite3.connect(str(db_path))
            try:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM items").fetchone()[0], 0)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM albums").fetchone()[0], 0)
            finally:
                c.close()

            # 3. Undo restores files, DB rows, and flex attributes
            rc, undo = run_bundled(["--undo", detail["manifest"], "--apply"])
            self.assertEqual(rc, 0, undo)
            self.assertTrue(undo["ok"])
            self.assertTrue(track_path.is_file())
            self.assertEqual(track_path.read_bytes(), b"fLaC-audio-data")
            self.assertTrue(art_path.is_file())
            self.assertTrue(nfo_path.is_file())

            c = sqlite3.connect(str(db_path))
            try:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM items WHERE id=10").fetchone()[0], 1)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM albums WHERE id=1").fetchone()[0], 1)
                self.assertEqual(
                    c.execute("SELECT value FROM item_attributes WHERE entity_id=10 AND key='rating'").fetchone()[0],
                    "5",
                )
                self.assertEqual(
                    c.execute("SELECT value FROM album_attributes WHERE entity_id=1 AND key='mood'").fetchone()[0],
                    "energetic",
                )
            finally:
                c.close()

            self.assertTrue(libops.removal(cfg, run_id)["undone_ts"])


if __name__ == "__main__":
    unittest.main()

"""End-to-end HTTP integration tests for Hunter Wave 3c Web API routes."""

from __future__ import annotations

import datetime
import http.client
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hunter import auth, beetsops, blocklist, config, db, domain, library, state
from hunter.web import common, start_ui
from hunter.web.routes import recovery as recovery_routes
from hunter.web.routes import wants as wants_routes


def _http_req(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = dict(headers or {})
    raw_body: bytes | None
    if isinstance(body, dict):
        raw_body = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    else:
        raw_body = body
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=raw_body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, data
    finally:
        conn.close()


def _create_beets_db(db_path: Path, music_root: Path) -> None:
    (music_root / "gnr").mkdir(parents=True, exist_ok=True)
    track_file = music_root / "gnr" / "01.mp3"
    track_file.write_bytes(b"ID3")

    conn = sqlite3.connect(str(db_path))
    try:
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
            """
        )
        conn.execute(
            "INSERT INTO albums (id, albumartist, albumartist_sort, album, year, original_year, "
            "genre, albumtype, albumstatus, albumdisambig, catalognum, mb_releasegroupid, "
            "mb_albumid, mb_albumartistid, comp, label, country, artpath, added) "
            "VALUES (1, 'Guns N'' Roses', 'Guns N'' Roses', 'Appetite', 1987, 1987, 'Rock', "
            "'album', 'Official', '', '', '11111111-1111-4111-8111-111111111111', 'mb-alb-1', "
            "'mb-art-1', 0, 'Geffen', 'US', NULL, 1000.0)"
        )
        conn.execute(
            "INSERT INTO items (id, album_id, artist, album, albumartist, title, disc, disctotal, "
            "track, tracktotal, length, bitrate, format, samplerate, bitdepth, channels, genre, "
            "bpm, initial_key, mb_trackid, mb_albumid, acoustid_id, acoustid_fingerprint, path, "
            "added, comments, lyrics, mtime) "
            "VALUES (1, 1, 'Guns N'' Roses', 'Appetite', 'Guns N'' Roses', 'Welcome', 1, 1, 1, 12, 200.0, "
            "192000, 'MP3', 44100, 16, 2, 'Rock', 120, 'Am', 'mb-t-1', 'mb-alb-1', '', '', ?, "
            "1000.0, '', '', 1000.0)",
            (str(track_file).encode("utf-8"),),
        )
        conn.commit()
    finally:
        conn.close()


class WebRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.music_dir = self.root / "music"
        self.music_dir.mkdir()
        self.quarantine_dir = self.root / "quarantine"
        self.quarantine_dir.mkdir()
        self.beets_dir = self.root / "beets"
        self.beets_dir.mkdir()
        self.beets_db = self.beets_dir / "library.db"
        _create_beets_db(self.beets_db, self.music_dir)
        self.docker_sock = self.root / "docker.sock"
        self.docker_sock.write_text("", encoding="utf-8")

        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "HUNTER_STATE_DIR": str(self.state_dir),
                "HUNTER_BEETS_DB": str(self.beets_db),
                "HUNTER_BEETS_AUTOMATION": str(self.beets_dir),
                "HUNTER_MUSIC_ROOT": str(self.music_dir),
                "HUNTER_QUARANTINE_DIR": str(self.quarantine_dir),
                "HUNTER_STORAGE_FSTYPE": "",
                "DOCKER_SOCK": str(self.docker_sock),
                "HUNTER_UI_TOKEN": "admin-secret-token",
                "NAVIDROME_USER": "nd-user",
                "NAVIDROME_PASS": "nd-pass",
            },
            clear=False,
        )
        self._env_patch.start()
        self._reset_caches()

        self.cfg = config.HunterConfig.from_env()
        db.open_hunter_db(self.cfg.state_db).close()

        a = auth.Auth(self.cfg.state_db)
        try:
            a.create_first_admin("admin1", "admin-pass-12345")
            a.create_user("op1", "op-pass-12345", "operator", created_by="admin1")
            a.create_user("view1", "view-pass-12345", "viewer", created_by="admin1")
        finally:
            a.close()

        self._spawn_patch = mock.patch.object(common, "spawn_pass")
        self._spawn_patch.start()

        self.server = start_ui(self.cfg, host="127.0.0.1", port=0)
        assert self.server is not None
        self.port = self.server.server_address[1]

        self.op_cookie = self._login("op1", "op-pass-12345")
        self.view_cookie = self._login("view1", "view-pass-12345")

    def tearDown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self._spawn_patch.stop()
        self._reset_caches()
        self._env_patch.stop()
        self._tmp.cleanup()

    def _reset_caches(self) -> None:
        db.reset_schema_cache()
        config.clear_config_cache()
        library.invalidate_library_cache()
        blocklist.invalidate()
        beetsops.cache.drop("watcher_config")
        with auth._fails_lock:
            auth._fails.clear()

    def _login(self, username: str, password: str) -> str:
        status, headers, _ = _http_req(
            self.port,
            "POST",
            "/api/auth/login",
            body={"username": username, "password": password},
            headers={"Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(status, 200)
        cookie = headers.get("set-cookie", "").split(";", 1)[0]
        self.assertTrue(cookie)
        return cookie

    def _op_headers(self, *, post: bool = False) -> dict[str, str]:
        h = {"Cookie": self.op_cookie}
        if post:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        return h

    def _view_headers(self, *, post: bool = False) -> dict[str, str]:
        h = {"Cookie": self.view_cookie}
        if post:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        return h

    def test_role_boundaries_operator_vs_viewer(self) -> None:
        for path in (
            "/api/beets/logs",
            "/api/jobs",
            "/api/settings",
            "/api/backups",
            "/api/services",
            "/api/recovery",
            "/api/spotify/auth_url",
            "/api/library/job_result?id=missing",
        ):
            status_v, _, _ = _http_req(self.port, "GET", path, headers=self._view_headers())
            self.assertEqual(status_v, 403, f"expected 403 for viewer on {path}, got {status_v}")
            if path in ("/api/library/job_result?id=missing", "/api/spotify/auth_url"):
                continue
            status_o, _, _ = _http_req(self.port, "GET", path, headers=self._op_headers())
            self.assertEqual(status_o, 200, f"expected 200 for operator on {path}, got {status_o}")

        status_bc_v, _, _ = _http_req(self.port, "GET", "/api/beets/config", headers=self._view_headers())
        self.assertEqual(status_bc_v, 403)
        with mock.patch("hunter.web.routes.beets.DockerClient") as m_docker:
            m_docker.return_value.available = True
            m_docker.return_value.env_of.return_value = {"BEETS_SOURCES": "slskd:album_per_dir:/downloads"}
            status_bc_o, _, body_bc_o = _http_req(
                self.port, "GET", "/api/beets/config", headers=self._op_headers()
            )
            self.assertEqual(status_bc_o, 200)
            data_bc = json.loads(body_bc_o)
            self.assertTrue(data_bc["ok"])
            self.assertEqual(data_bc["rows"][0]["key"], "BEETS_SOURCES")

        for path in ("/api/library/overview", "/api/wants", "/api/status"):
            status_v, _, _ = _http_req(self.port, "GET", path, headers=self._view_headers())
            self.assertEqual(status_v, 200, f"expected 200 for viewer on {path}, got {status_v}")

    def test_library_modify_confirm_count_gate(self) -> None:
        with (
            mock.patch("hunter.web.routes.library.DockerClient") as m_docker,
            mock.patch.object(common, "jobs") as m_jobs,
        ):
            m_jobs.return_value.start_beets.return_value = {"ok": True, "job": {"id": "job-small"}}
            # Targeted small edit (count <= SOFT_MAX) runs without confirm_count
            m_docker.return_value.exec_run.return_value = {
                "exit_code": 0,
                "output": "1\tA\tB\n2\tC\tD\n__HUNTER_COUNT__=2\n",
            }
            st_small, _, data_small = _http_req(
                self.port,
                "POST",
                "/api/library/modify",
                body={"query": "artist:Test", "mods": {"genre": "Rock"}, "apply": True},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_small, 200)
            self.assertEqual(json.loads(data_small)["job"]["id"], "job-small")

            # Bulk edit (> SOFT_MAX=200) requires confirm_count -> 409 then 200
            m_docker.return_value.exec_run.return_value = {
                "exit_code": 0,
                "output": "1\tA\tB\n__HUNTER_COUNT__=250\n",
            }
            st_409, _, data_409 = _http_req(
                self.port,
                "POST",
                "/api/library/modify",
                body={"query": "artist:Test", "mods": {"genre": "Rock"}, "apply": True},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_409, 409)
            payload_409 = json.loads(data_409)
            self.assertEqual(payload_409["count"], 250)
            self.assertTrue(payload_409["needs_confirm"])

            m_jobs.return_value.start_beets.return_value = {"ok": True, "job": {"id": "job-1"}}
            st_200, _, data_200 = _http_req(
                self.port,
                "POST",
                "/api/library/modify",
                body={
                    "query": "artist:Test",
                    "mods": {"genre": "Rock"},
                    "apply": True,
                    "confirm_count": 250,
                },
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_200, 200)
            self.assertEqual(json.loads(data_200)["job"]["id"], "job-1")
            self.assertEqual(m_jobs.return_value.start_beets.call_count, 2)

    def test_library_beet_apostrophe_quoting(self) -> None:
        with (
            mock.patch("hunter.web.routes.library.DockerClient") as m_docker,
            mock.patch.object(common, "jobs") as m_jobs,
        ):
            m_docker.return_value.exec_run.return_value = {
                "exit_code": 0,
                "output": "1\tGuns N' Roses\tAppetite\n__HUNTER_COUNT__=1\n",
            }
            m_jobs.return_value.start_beets.return_value = {"ok": True, "job": {"id": "job-gnr"}}
            st_code, _, raw = _http_req(
                self.port,
                "POST",
                "/api/library/beet",
                body={"artist": "Guns N' Roses", "action": "write"},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_code, 200)
            self.assertEqual(json.loads(raw)["job"]["id"], "job-gnr")
            _, kwargs = m_jobs.return_value.start_beets.call_args
            argv = kwargs["argv"]
            self.assertIn("write", argv)
            self.assertIn("albumartist:Guns N' Roses", argv)

    def test_library_want_sets_operator_priority(self) -> None:
        st_code, _, raw = _http_req(
            self.port,
            "POST",
            "/api/library/want",
            body={"album_id": 1, "kind": "gapfill"},
            headers=self._op_headers(post=True),
        )
        self.assertEqual(st_code, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["added"])
        st = state.HunterState(self.cfg.state_db)
        try:
            wid = st.want_id_by_key("gapfill:beets:1")
            self.assertIsNotNone(wid)
            assert wid is not None
            row = st.want_row(wid)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["priority"], domain.PRIORITY_OPERATOR)
        finally:
            st.close()

    def test_make_playlist_returns_name_and_guards_existing(self) -> None:
        tracks = [{"artist": "Guns N' Roses", "title": "Welcome"}]
        with mock.patch("hunter.playlist_sync.SubsonicClient") as m_sub:
            inst = mock.MagicMock()
            m_sub.return_value.__enter__.return_value = inst
            inst.playlists_multi.return_value = {}
            inst.search_songs.return_value = [
                {"id": "s-1", "title": "Welcome", "artist": "Guns N' Roses", "duration": 200}
            ]
            inst.create_or_replace.return_value = "nd-1"

            st_ok, _, raw_ok = _http_req(
                self.port,
                "POST",
                "/api/make_playlist",
                body={"name": "My Mix", "tracks": tracks},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_ok, 200)
            body_ok = json.loads(raw_ok)
            self.assertEqual(body_ok["name"], "My Mix")
            self.assertTrue(body_ok["ok"])
            self.assertEqual(body_ok["total"], 1)
            self.assertEqual(body_ok["matched"], 1)

            st_verify = state.HunterState(self.cfg.state_db)
            try:
                nd_map = json.loads(st_verify.get_meta("nd_playlists", "{}") or "{}")
                self.assertEqual(nd_map.get("My Mix"), "nd-1")
            finally:
                st_verify.close()

            inst.playlists_multi.return_value = {"Existing": ["foreign-id"]}
            inst.create_or_replace.reset_mock()
            st_conflict, _, raw_conflict = _http_req(
                self.port,
                "POST",
                "/api/make_playlist",
                body={"name": "Existing", "tracks": tracks},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_conflict, 409)
            self.assertIn("not created by hunter", json.loads(raw_conflict)["error"].lower())
            inst.create_or_replace.assert_not_called()

    def test_calendar_ics_escapes_rfc5545_chars(self) -> None:
        today = datetime.date.today().isoformat()
        st = state.HunterState(self.cfg.state_db)
        try:
            st.upsert_calendar(
                "22222222-2222-4222-8222-222222222222",
                "mb-a1",
                "A, B; C\\D\nE",
                "Test, Album; Vol\\1",
                today,
                "Album",
            )
        finally:
            st.close()

        st_code, hdrs, raw = _http_req(self.port, "GET", "/calendar.ics", headers=self._view_headers())
        self.assertEqual(st_code, 200)
        self.assertIn("text/calendar", hdrs.get("content-type", ""))
        text = raw.decode("utf-8")
        self.assertIn("SUMMARY:A\\, B\\; C\\\\D E — Test\\, Album\\; Vol\\\\1", text)

    def test_recovery_requeue_uses_album_track_total_and_operator_priority(self) -> None:
        mock_albums = [
            {
                "key": "a1",
                "album_id": 10,
                "artist": "Art One",
                "album": "Alb One",
                "year": 2020,
                "tracks": 1,
                "album_track_total": 12,
                "bytes": 100,
                "item_ids": [1],
            },
            {
                "key": "a2",
                "album_id": 11,
                "artist": "Art Two",
                "album": "Alb Two",
                "year": 2021,
                "tracks": 2,
                "album_track_total": 8,
                "bytes": 200,
                "item_ids": [2, 3],
            },
        ]
        with (
            mock.patch.object(recovery_routes.storage, "summary", return_value={"ok": True}),
            mock.patch.object(recovery_routes.recovery, "scan", return_value={"trusted": True}),
            mock.patch.object(recovery_routes.recovery, "verify", return_value={"ok": True}),
            mock.patch.object(recovery_routes.recovery, "albums_of", return_value=mock_albums),
            mock.patch.object(common, "state", wraps=common.state) as m_state,
        ):
            st_code, _, raw = _http_req(
                self.port,
                "POST",
                "/api/recovery/requeue",
                body={"item_ids": [1, 2, 3]},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_code, 200)
            payload = json.loads(raw)
            self.assertEqual(payload["queued"], 2)
            self.assertEqual(m_state.call_count, 1)

        st = state.HunterState(self.cfg.state_db)
        try:
            wid1 = st.want_id_by_key("recovery:a1")
            wid2 = st.want_id_by_key("recovery:a2")
            self.assertIsNotNone(wid1)
            self.assertIsNotNone(wid2)
            assert wid1 is not None and wid2 is not None
            w1 = st.want_row(wid1)
            w2 = st.want_row(wid2)
            self.assertIsNotNone(w1)
            self.assertIsNotNone(w2)
            assert w1 is not None and w2 is not None
            self.assertEqual(w1["track_count"], 12)
            self.assertEqual(w2["track_count"], 8)
            self.assertEqual(w1["priority"], domain.PRIORITY_OPERATOR)
            self.assertEqual(w2["priority"], domain.PRIORITY_OPERATOR)
        finally:
            st.close()

    def test_beets_preview_single_pass_and_nonzero_exit_code(self) -> None:
        with mock.patch("hunter.web.routes.beets.DockerClient") as m_docker:
            m_docker.return_value.exec_run.return_value = {
                "exit_code": 2,
                "output": "1\tArt\tAlb\n__HUNTER_COUNT__=1\nerror: bad query\n",
            }
            st_code, _, raw = _http_req(
                self.port,
                "POST",
                "/api/beets/preview",
                body={"command": "list", "query": "artist:Art"},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_code, 200)
            body = json.loads(raw)
            self.assertEqual(m_docker.return_value.exec_run.call_count, 1)
            self.assertEqual(body["exit_code"], 2)
            self.assertEqual(body["count"], 1)

            # Previewing non-dry-run mutate commands (fetchart/scrub) succeeds with 200
            m_docker.return_value.exec_run.return_value = {
                "exit_code": 0,
                "output": "1\tArt\tAlb\n__HUNTER_COUNT__=1\n",
            }
            for cmd_name in ("fetchart", "scrub"):
                st_m, _, raw_m = _http_req(
                    self.port,
                    "POST",
                    "/api/beets/preview",
                    body={"command": cmd_name, "query": "artist:Art"},
                    headers=self._op_headers(post=True),
                )
                self.assertEqual(st_m, 200, f"expected 200 previewing {cmd_name}: {raw_m!r}")

            # Previewing pretend-capable mutate command (move) with empty query shows needs_confirm=True
            st_mv, _, raw_mv = _http_req(
                self.port,
                "POST",
                "/api/beets/preview",
                body={"command": "move", "query": ""},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_mv, 200)
            mv_body = json.loads(raw_mv)
            self.assertTrue(mv_body["needs_confirm"])
            self.assertNotIn("-p", mv_body["argv"])

    def test_wants_mb_unavailable_returns_503(self) -> None:
        with mock.patch.object(
            wants_routes.mb, "artist_release_groups", side_effect=wants_routes.mb.MBUnavailable("down")
        ):
            st_1, _, raw_1 = _http_req(
                self.port,
                "POST",
                "/api/request_artist",
                body={"id": "11111111-1111-4111-8111-111111111111", "name": "Test Artist"},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_1, 503)
            self.assertTrue(json.loads(raw_1).get("retry"))

        with mock.patch.object(
            wants_routes.mb, "mb_get", side_effect=wants_routes.mb.MBUnavailable("down")
        ):
            st_2, _, raw_2 = _http_req(
                self.port,
                "POST",
                "/api/resolve_url",
                body={"url": "https://musicbrainz.org/release-group/11111111-1111-4111-8111-111111111111"},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_2, 503)
            self.assertTrue(json.loads(raw_2).get("retry"))

    def test_art_route_validates_uuid_content_type_size_and_private_cache(self) -> None:
        st_bad_uuid, _, _ = _http_req(
            self.port, "GET", "/api/art/not-a-uuid", headers=self._view_headers()
        )
        self.assertEqual(st_bad_uuid, 400)

        rgid = "33333333-3333-4333-8333-333333333333"
        fake_bad_ct = mock.MagicMock()
        fake_bad_ct.status_code = 200
        fake_bad_ct.headers = {"Content-Type": "text/html"}
        fake_bad_ct.iter_content.return_value = [b"<html>nope</html>"]

        fake_big = mock.MagicMock()
        fake_big.status_code = 200
        fake_big.headers = {"Content-Type": "image/jpeg"}
        fake_big.iter_content.return_value = [b"x" * (5 * 1024 * 1024 + 1)]

        fake_ok = mock.MagicMock()
        fake_ok.status_code = 200
        fake_ok.headers = {"Content-Type": "image/jpeg"}
        fake_ok.iter_content.return_value = [b"\xff\xd8\xff\xe0TESTJPEG"]

        fake_sess = mock.MagicMock()
        fake_sess.get.side_effect = [fake_bad_ct, fake_bad_ct]
        with mock.patch.object(common.ART_CLIENT, "session", return_value=fake_sess):
            st_bad_ct, _, _ = _http_req(
                self.port, "GET", f"/api/art/{rgid}", headers=self._view_headers()
            )
            self.assertEqual(st_bad_ct, 404)

        (self.state_dir / "art" / f"{rgid}.miss").unlink(missing_ok=True)
        fake_sess.get.side_effect = [fake_big, fake_big]
        with mock.patch.object(common.ART_CLIENT, "session", return_value=fake_sess):
            st_big, _, _ = _http_req(
                self.port, "GET", f"/api/art/{rgid}", headers=self._view_headers()
            )
            self.assertEqual(st_big, 404)

        (self.state_dir / "art" / f"{rgid}.miss").unlink(missing_ok=True)
        fake_sess.get.side_effect = [fake_ok]
        with mock.patch.object(common.ART_CLIENT, "session", return_value=fake_sess):
            st_ok, hdrs_ok, raw_ok = _http_req(
                self.port, "GET", f"/api/art/{rgid}", headers=self._view_headers()
            )
            self.assertEqual(st_ok, 200)
            self.assertEqual(hdrs_ok.get("cache-control"), "private, max-age=86400")
            self.assertEqual(raw_ok, b"\xff\xd8\xff\xe0TESTJPEG")
            self.assertTrue((self.state_dir / "art" / f"{rgid}.jpg").exists())

    def test_est_bytes_preflight_spotify_offline_beets_malformed_types_and_head(self) -> None:
        # 1. remove/plan passes est_bytes (not 0) to removal_preflight
        fake_plan = {
            "empty": False,
            "targets": {"artists": [], "album_ids": [1], "item_ids": []},
            "albums": [{"album_id": 1, "full": True}],
            "all_album_ids": [1],
            "tracks": 1,
            "est_bytes": 42_000,
            "fingerprint": "fp1",
            "too_many": False,
        }
        with (
            mock.patch("hunter.web.routes.library.libops.removal_plan", return_value=fake_plan),
            mock.patch("hunter.web.routes.library.libops.removal_preflight", return_value={"ok": True}) as m_pf,
        ):
            st_p, _, _ = _http_req(
                self.port,
                "POST",
                "/api/library/remove/plan",
                body={"album_ids": [1]},
                headers=self._op_headers(post=True),
            )
            self.assertEqual(st_p, 200)
            m_pf.assert_called_once_with(mock.ANY, need_bytes=42_000)

        # 2. playlist_tracks handles missing beets DB cleanly (no KeyError 500)
        st = state.HunterState(self.cfg.state_db)
        try:
            st.upsert_playlist("pl-off", "Offline Mix", 1)
            st.sp_upsert_track("tr-off", "Welcome", "Guns N' Roses", "Appetite", "sp-alb", 1987, 200)
            st.sp_link_track("pl-off", "tr-off", 0, 1)
            st.sp_playlist_done("pl-off", 1, 1)
        finally:
            st.close()
        with mock.patch.object(common, "library", return_value=None):
            st_pt, _, raw_pt = _http_req(
                self.port,
                "GET",
                "/api/spotify/playlist_tracks?id=pl-off",
                headers=self._view_headers(),
            )
            self.assertEqual(st_pt, 200)
            pt_body = json.loads(raw_pt)
            self.assertIn("beets_album_id", pt_body["albums"][0])

        # 3. Malformed JSON types & invalid combos return 400, never 500
        bad_requests = [
            ("/api/hunter/run", {"command": "seed", "flags": "not-a-list"}),
            ("/api/hunter/run", {"command": "seed", "ints": {"--seed-limit": True}}),
            ("/api/recovery/requeue", {"item_ids": "bad"}),
            ("/api/recovery/requeue", {"item_ids": ["--5"]}),
            ("/api/grab", {"id": 1, "username": "u", "files": ["not-a-dict"]}),
            ("/api/pass", {"dry_run": True, "apply": True}),
        ]
        for path, body in bad_requests:
            st_bad, _, _ = _http_req(
                self.port, "POST", path, body=body, headers=self._op_headers(post=True)
            )
            self.assertEqual(st_bad, 400, f"expected 400 on {path} {body!r}, got {st_bad}")

        # 4. HEAD /api/downloads/file must not call mark_served()
        dl_dir = self.state_dir / "downloads"
        dl_dir.mkdir(parents=True, exist_ok=True)
        zip_path = dl_dir / "bundle.zip"
        zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        fake_bundle = {
            "id": "b1",
            "title": "Test",
            "status": "ready",
            "requested_by": "view1",
            "path": str(zip_path),
        }
        with (
            mock.patch("hunter.downloads.get", return_value=fake_bundle),
            mock.patch("hunter.downloads.mark_served") as m_served,
        ):
            st_head, _, _ = _http_req(
                self.port, "HEAD", "/api/downloads/file?id=b1", headers=self._view_headers()
            )
            self.assertEqual(st_head, 200)
            m_served.assert_not_called()


if __name__ == "__main__":
    unittest.main()

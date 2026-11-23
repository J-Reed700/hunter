"""Portability & standalone-boot tests for Hunter."""
from __future__ import annotations

import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hunter
from hunter import beetsops, db, http_client, storage
from hunter.config import HunterConfig, clear_config_cache
from hunter.dockerctl import DockerClient
from hunter.jobs import Jobs
from hunter.web import server as web_server


def _http_req(
    port: int,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        hdrs = dict(headers or {})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read().decode("utf-8", "replace")
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


class PortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.music_dir = self.root / "music"
        self.state_dir.mkdir(parents=True)
        self.music_dir.mkdir(parents=True)
        db.reset_schema_cache()
        clear_config_cache()
        self.addCleanup(db.reset_schema_cache)
        self.addCleanup(clear_config_cache)

    def test_fresh_standalone_boot_with_only_state_and_music_root(self) -> None:
        env = {
            "HUNTER_STATE_DIR": str(self.state_dir),
            "HUNTER_MUSIC_ROOT": str(self.music_dir),
            "HUNTER_UI_PORT": "0",
        }
        orig_exists = Path.exists

        def _exists_no_quarantine(p: Path) -> bool:
            if str(p) == "/quarantine":
                return False
            return orig_exists(p)

        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(Path, "exists", autospec=True, side_effect=_exists_no_quarantine),
        ):
            cfg = HunterConfig.from_env()

        self.assertIsNone(cfg.storage_fstype)
        self.assertEqual(cfg.slskd_url, "http://slskd:5030")
        self.assertEqual(cfg.music_root, self.music_dir)
        self.assertFalse(cfg.quarantine_enabled)
        self.assertEqual(cfg.quarantine_dir, Path("/quarantine"))
        self.assertEqual(cfg.beets_exec_user, "")
        self.assertEqual(cfg.beets_python, "python3")
        self.assertEqual(cfg.beets_cli, ("beet",))
        self.assertEqual(cfg.beets_automation_container_dir, "/config/automation")
        self.assertEqual(cfg.hunter_container, "hunter")
        self.assertEqual(cfg.slskd_container, "slskd")
        self.assertEqual(cfg.navidrome_container, "navidrome")
        self.assertEqual(cfg.prowlarr_container, "prowlarr")
        self.assertEqual(cfg.services, ())
        self.assertIsNone(cfg.schedule_file)

        # storage.summary passes on a normal host filesystem without mergerfs or /quarantine
        st_summary = storage.summary(cfg)
        self.assertTrue(st_summary["ok"], st_summary)
        self.assertFalse(st_summary["quarantine"]["configured"])
        self.assertIsNone(st_summary["quarantine"]["error"])

        # Missing automation scripts / schedule return empty lists cleanly
        jobs = Jobs(cfg)
        self.assertEqual(beetsops.script_catalogue(jobs, cfg), {"scripts": []})
        self.assertEqual(beetsops.load_schedule(cfg), [])

        # Generic User-Agent with no personal/server branding
        with mock.patch.dict(os.environ, {}, clear=True):
            ua = http_client.default_user_agent()
        self.assertTrue(ua.startswith(f"Hunter-hunter/{hunter.__version__}"))
        self.assertNotIn("TheGallows", ua)

        # Boot the HTTP server in standalone setup mode and probe core endpoints
        db.open_hunter_db(cfg.state_db).close()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(Path, "exists", autospec=True, side_effect=_exists_no_quarantine),
        ):
            clear_config_cache()
            srv = web_server.start_ui(cfg, host="127.0.0.1", port=0)
            self.assertIsNotNone(srv)
            assert srv is not None
            try:
                port = srv.server_address[1]
                code_me, _, body_me = _http_req(port, "GET", "/api/auth/me")
                self.assertEqual(code_me, 200, body_me)
                self.assertTrue(json.loads(body_me).get("setup_required"))

                code_setup, hdrs_setup, body_setup = _http_req(
                    port,
                    "POST",
                    "/api/auth/setup",
                    body={"username": "admin1", "password": "strong-password-123"},
                    headers={"Origin": f"http://127.0.0.1:{port}"},
                )
                self.assertEqual(code_setup, 200, body_setup)
                cookie = hdrs_setup.get("Set-Cookie", "").split(";", 1)[0]
                self.assertTrue(cookie)

                code_st, _, body_st = _http_req(
                    port, "GET", "/api/status", headers={"Cookie": cookie}
                )
                self.assertEqual(code_st, 200, body_st)
                data_st = json.loads(body_st)
                self.assertTrue(data_st["storage"]["ok"])
                self.assertFalse(data_st["audiomuse"])
                self.assertEqual(data_st["music_root"], str(self.music_dir))
                self.assertIsNone(data_st["quarantine_dir"])

                # /api/services filters absent optional containers (always keeping hunter_container)
                sock_file = self.root / "docker.sock"
                sock_file.write_text("")
                with (
                    mock.patch.dict(
                        os.environ,
                        {**env, "DOCKER_SOCK": str(sock_file)},
                        clear=True,
                    ),
                    mock.patch("hunter.dockerctl.DockerClient.state") as m_state,
                ):
                    clear_config_cache()
                    m_state.side_effect = lambda n: {
                        "name": n,
                        "exists": n == "slskd",
                        "running": n == "slskd",
                        "status": "running" if n == "slskd" else "absent",
                        "health": None,
                        "since": None,
                    }
                    code_sv, _, body_sv = _http_req(
                        port, "GET", "/api/services", headers={"Cookie": cookie}
                    )
                    self.assertEqual(code_sv, 200, body_sv)
                    sv_names = [s["name"] for s in json.loads(body_sv)["services"]]
                    self.assertEqual(sv_names, ["hunter", "slskd"])

                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            **env,
                            "DOCKER_SOCK": str(sock_file),
                            "HUNTER_SERVICES": "custom-a,custom-b",
                        },
                        clear=True,
                    ),
                    mock.patch("hunter.dockerctl.DockerClient.state") as m_state,
                ):
                    clear_config_cache()
                    m_state.side_effect = lambda n: {
                        "name": n,
                        "exists": False,
                        "running": False,
                        "status": "absent",
                        "health": None,
                        "since": None,
                    }
                    code_sv2, _, body_sv2 = _http_req(
                        port, "GET", "/api/services", headers={"Cookie": cookie}
                    )
                    self.assertEqual(code_sv2, 200, body_sv2)
                    sv2_names = [s["name"] for s in json.loads(body_sv2)["services"]]
                    self.assertEqual(sv2_names, ["custom-a", "custom-b"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_quarantine_enabled_env_and_docker_exec_empty_user(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HUNTER_STATE_DIR": str(self.state_dir),
                "HUNTER_MUSIC_ROOT": str(self.music_dir),
                "HUNTER_QUARANTINE_DIR": "",
            },
            clear=True,
        ):
            clear_config_cache()
            cfg = HunterConfig.from_env()
            self.assertFalse(cfg.quarantine_enabled)

        q_dir = self.root / "q"
        q_dir.mkdir()
        with mock.patch.dict(
            os.environ,
            {
                "HUNTER_STATE_DIR": str(self.state_dir),
                "HUNTER_MUSIC_ROOT": str(self.music_dir),
                "HUNTER_QUARANTINE_DIR": str(q_dir),
            },
            clear=True,
        ):
            clear_config_cache()
            cfg2 = HunterConfig.from_env()
            self.assertTrue(cfg2.quarantine_enabled)
            self.assertEqual(cfg2.quarantine_dir, q_dir)

        # DockerClient.exec_run omits "User" when user is empty
        dc = DockerClient(str(self.root / "unused.sock"))
        captured: list[dict] = []

        def fake_req(method, path, payload=None, timeout=None, max_bytes=16 * 1024 * 1024):
            if path.endswith("/exec") and isinstance(payload, dict):
                captured.append(payload)
                return 201, json.dumps({"Id": "e1"}).encode()
            if path.endswith("/start"):
                return 200, b""
            if path.endswith("/json"):
                return 200, json.dumps({"Running": False, "ExitCode": 0}).encode()
            return 200, b"{}"

        with mock.patch.object(dc, "_request", side_effect=fake_req):
            dc.exec_run("beets", ["beet", "version"], user="")
            dc.exec_run("beets", ["beet", "version"], user="1000:1000")
        self.assertNotIn("User", captured[0])
        self.assertEqual(captured[1]["User"], "1000:1000")

    def test_no_personal_or_server_specific_strings_in_hunter_package(self) -> None:
        pkg_root = Path(__file__).resolve().parent.parent / "hunter"
        forbidden = (
            "TheGallows",
            "jreed",
            "josh",
            "gluetun",
            "beets-cron",
            "music-ops",
            "fatfile_shrink",
            "subtitle_backfill",
            "user :972",
            "2026-09-02",
        )
        hits: list[str] = []
        for path in sorted(pkg_root.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix not in {".py", ".js", ".css", ".html", ".json", ".txt", ".md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(pkg_root)}: {token}")
        self.assertEqual(hits, [], f"Forbidden server/personal strings found: {hits}")


if __name__ == "__main__":
    unittest.main()

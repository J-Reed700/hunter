"""Unit tests for hunter.cli."""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from hunter import cli
from hunter.auth import Auth
from hunter.config import HunterConfig, clear_config_cache
from hunter.db import reset_schema_cache
from hunter.state import HunterState


def _env_for(tmp: Path) -> dict[str, str]:
    return {
        "HUNTER_STATE_DIR": str(tmp),
        "HUNTER_BEETS_DB": str(tmp / "musiclibrary.db"),
        "HUNTER_NAVIDROME_DB": str(tmp / "navidrome.db"),
        "HUNTER_MUSIC_ROOT": str(tmp / "merged"),
        "HUNTER_QUARANTINE_DIR": str(tmp / "quarantine"),
        "HUNTER_STORAGE_FSTYPE": "",
        "SPOTIFY_CLIENT_ID": "cid",
        "SPOTIFY_CLIENT_SECRET": "csec",
        "SPOTIFY_PROXY": "http://proxy:8888",
    }


def _make_beets_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT, album TEXT, mb_releasegroupid TEXT);
        CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, disc INTEGER, disctotal INTEGER DEFAULT 1, tracktotal INTEGER, format TEXT);
        INSERT INTO albums VALUES (1, 'Portishead', 'Dummy', 'rg1');
        INSERT INTO items VALUES (1, 1, 1, 1, 1, 'FLAC');
        """
    )
    conn.commit()
    conn.close()


class TestCli(unittest.TestCase):
    def tearDown(self) -> None:
        clear_config_cache()
        reset_schema_cache()
        root = logging.getLogger()
        for h in list(root.handlers):
            if hasattr(h, "close"):
                h.close()
        root.handlers.clear()
        if hasattr(root, "_hunter_configured"):
            delattr(root, "_hunter_configured")

    def test_health_runs_before_logging_and_checks_crashed_and_streaks(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "merged").mkdir()
            (tmp / "quarantine").mkdir()
            (tmp / "heartbeat.json").write_text(
                json.dumps({"ts": int(time.time()), "status": "ok"}), encoding="utf-8"
            )
            (tmp / "supervisor.json").write_text(
                json.dumps({"consecutive_failures": 0, "lock_busy_streak": 0}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, _env_for(tmp), clear=False):
                clear_config_cache()
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = cli.main(["health"])
                self.assertEqual(rc, 0)
                data = json.loads(buf.getvalue())
                self.assertTrue(data["ok"])
                # Must NOT create log dir/files on healthcheck
                self.assertFalse((tmp / "logs").exists())

                # Crashed heartbeat or lock_busy_streak >= 4 -> unhealthy
                (tmp / "heartbeat.json").write_text(
                    json.dumps({"ts": int(time.time()), "status": "crashed", "error": "boom"}),
                    encoding="utf-8",
                )
                (tmp / "supervisor.json").write_text(
                    json.dumps({"consecutive_failures": 0, "lock_busy_streak": 4}),
                    encoding="utf-8",
                )
                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    rc2 = cli.main(["health"])
                self.assertEqual(rc2, 1)
                data2 = json.loads(buf2.getvalue())
                self.assertFalse(data2["ok"])
                self.assertEqual(len(data2["problems"]), 2)

    def test_mutually_exclusive_apply_and_dry_run(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["pass", "--apply", "--dry-run"])
        self.assertEqual(ctx.exception.code, 2)

    def test_add_owned_gate_force_and_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _make_beets_db(tmp / "musiclibrary.db")
            with patch.dict(os.environ, _env_for(tmp), clear=False):
                clear_config_cache()
                # Owned -> skipped without --force
                out1 = io.StringIO()
                with redirect_stdout(out1):
                    rc1 = cli.main(["add", "Portishead", "Dummy"])
                self.assertEqual(rc1, 0)
                self.assertIn("already owned", out1.getvalue())

                # --force queues and logs event
                out2 = io.StringIO()
                with redirect_stdout(out2):
                    rc2 = cli.main(["add", "Portishead", "Dummy", "--force"])
                self.assertEqual(rc2, 0)
                self.assertIn("added:", out2.getvalue())
                with HunterState(tmp / "hunter.db") as st:
                    self.assertIsNotNone(st.want_by_key("manual:portishead:dummy"))
                    evs = st.events(limit=5)
                    self.assertTrue(any(e["kind"] == "request" for e in evs))

    def test_auth_preserves_sp_dc_and_uses_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "spotify_token.json").write_text(
                json.dumps({"sp_dc": "keep_cookie"}), encoding="utf-8"
            )
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"refresh_token": "rt_new"}
            with patch.dict(os.environ, _env_for(tmp), clear=False), \
                 patch("builtins.input", return_value="http://127.0.0.1:8888/callback?code=abc"), \
                 patch("hunter.spotify.HttpSession") as mock_http_cls:
                mock_http_cls.return_value.__enter__.return_value.post.return_value = mock_resp
                clear_config_cache()
                rc = cli.main(["auth"])
                self.assertEqual(rc, 0)
                self.assertEqual(
                    mock_http_cls.call_args.kwargs["proxy"],
                    "http://proxy:8888",
                )
            tok = json.loads((tmp / "spotify_token.json").read_text(encoding="utf-8"))
            self.assertEqual(tok["sp_dc"], "keep_cookie")
            self.assertEqual(tok["refresh_token"], "rt_new")

    def test_add_blocklisted_artist_rejected_before_logging_and_seed_gaps_coverage_default(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            auto_lib = tmp / "auto" / "lib"
            auto_lib.mkdir(parents=True)
            (auto_lib / "artist_blocklist.txt").write_text("Blocked Band\n", encoding="utf-8")
            env = {**_env_for(tmp), "HUNTER_BEETS_AUTOMATION": str(tmp / "auto"), "HUNTER_OWNED_COVERAGE": "0.9"}
            with patch.dict(os.environ, env, clear=False):
                clear_config_cache()
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = cli.main(["add", "Blocked Band", "Album"])
                self.assertEqual(rc, 1)
                self.assertIn("blocked by artist blocklist", out.getvalue())
                if (tmp / "hunter.db").exists():
                    with HunterState(tmp / "hunter.db") as st:
                        self.assertEqual(st.events(limit=10), [])

                # seed-gaps --coverage defaults to None (falls back to cfg.owned_coverage)
                ap = cli._build_parser()
                args = ap.parse_args(["seed-gaps"])
                self.assertIsNone(args.coverage)

    def test_user_add_audits_and_role_supports_positional_and_flag(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with patch.dict(os.environ, _env_for(tmp), clear=False):
                clear_config_cache()
                self.assertEqual(
                    cli.main(["user", "add", "alice", "--role", "viewer", "--password", "Passw0rd123!"]),
                    0,
                )
                self.assertEqual(cli.main(["user", "role", "alice", "operator"]), 0)
                self.assertEqual(cli.main(["user", "role", "alice", "--role", "admin"]), 0)
                with Auth(tmp / "hunter.db") as a:
                    u = a.user_by_name("alice")
                    self.assertEqual(u["role"], "admin")
                    aud = a.audit_rows(limit=10)["rows"]
                    actions = [r["action"] for r in aud]
                    self.assertIn("cli.user.add", actions)
                    self.assertIn("cli.user.role", actions)


if __name__ == "__main__":
    unittest.main()

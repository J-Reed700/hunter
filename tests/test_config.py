"""Tests for hunter.config (env parsing, validation, tunables, secrets, cache)."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hunter import config as cfg_mod
from hunter.config import (
    HunterConfig,
    clear_config_cache,
    parse_tunable,
    save_apply_override,
    save_spotify_app,
    save_spotify_token,
    save_tunables,
)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        clear_config_cache()

    def tearDown(self):
        clear_config_cache()
        self.tmp.cleanup()

    def _env(self, **extra):
        base = {
            "HUNTER_STATE_DIR": str(self.state_dir),
        }
        base.update(extra)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_env_str_strips_whitespace_and_reads_new_vars(self):
        with self._env(
            SPOTIFY_PROXY="  http://proxy.local:8888  ",
            HUNTER_STORAGE_FSTYPE="   ",
            HUNTER_TRUSTED_PROXIES=" 10.0.0.0/8 , 192.168.0.0/16 ",
            HUNTER_USER_AGENT_CONTACT=" ops@example.com ",
            HUNTER_BACKOFF_SECONDS="60, 120, 300",
            HUNTER_MISS_RETRY_SECONDS="3600",
            HUNTER_DL_MAX_GB="10",
            HUNTER_DL_KEEP_GB="20",
            HUNTER_DL_TTL_HOURS="12",
            HUNTER_DL_MAX_FILES="500",
        ):
            cfg = HunterConfig.from_env()
            self.assertEqual(cfg.spotify_proxy, "http://proxy.local:8888")
            self.assertIsNone(cfg.storage_fstype)
            self.assertEqual(cfg.trusted_proxies, ("10.0.0.0/8", "192.168.0.0/16"))
            self.assertEqual(cfg.user_agent_contact, "ops@example.com")
            self.assertEqual(cfg.backoff_seconds, (60, 120, 300))
            self.assertEqual(cfg.miss_retry_seconds, 3600)
            self.assertEqual(cfg.dl_max_bundle_bytes, 10 * (1 << 30))
            self.assertEqual(cfg.dl_keep_bytes, 20 * (1 << 30))
            self.assertEqual(cfg.dl_ttl_seconds, 12 * 3600)
            self.assertEqual(cfg.dl_max_files, 500)

    def test_non_dict_json_files_do_not_crash(self):
        (self.state_dir / "settings.json").write_text('["not", "a", "dict"]')
        (self.state_dir / "spotify_app.json").write_text('"str"')
        (self.state_dir / "spotify_token.json").write_text("12345")
        with self._env():
            cfg = HunterConfig.from_env()
            self.assertEqual(cfg.interval, 1800)
            self.assertEqual(cfg.spotify_client_id, "")
            self.assertFalse(cfg.spotify_configured)

    def test_pair_consistent_spotify_creds(self):
        save_spotify_app(
            HunterConfig.from_env() if False else type("C", (), {"state_dir": self.state_dir})(),
            "file_id",
            "file_secret",
        )
        # Incomplete env pair must NOT mix env id with file secret
        with self._env(SPOTIFY_CLIENT_ID="env_only_id"):
            cfg = HunterConfig.from_env()
            self.assertEqual((cfg.spotify_client_id, cfg.spotify_client_secret), ("file_id", "file_secret"))
        # Complete env pair wins
        with self._env(SPOTIFY_CLIENT_ID="env_id", SPOTIFY_CLIENT_SECRET="env_secret"):
            cfg = HunterConfig.from_env()
            self.assertEqual((cfg.spotify_client_id, cfg.spotify_client_secret), ("env_id", "env_secret"))

    def test_parse_tunable_rejects_bool_and_non_int_float(self):
        self.assertEqual(parse_tunable("max_grabs_per_pass", 10), 10)
        self.assertEqual(parse_tunable("prefer_lossless", False), False)
        with self.assertRaises(ValueError):
            parse_tunable("max_grabs_per_pass", True)
        with self.assertRaises(ValueError):
            parse_tunable("max_grabs_per_pass", 3.5)
        with self.assertRaises(ValueError):
            parse_tunable("max_grabs_per_pass", 999)
        with self.assertRaises(ValueError):
            parse_tunable("prefer_lossless", "true")
        with self.assertRaises(ValueError):
            parse_tunable("unknown_key", 1)

    def test_atomic_savers_and_token_merge(self):
        with self._env():
            cfg = HunterConfig.from_env()
            applied = save_tunables(cfg, {"interval": 600, "max_grabs_per_pass": True, "bogus": 1})
            self.assertEqual(applied, {"interval": 600})
            save_apply_override(cfg, True)
            save_spotify_app(cfg, "cid", "csec")
            self.assertEqual(stat.S_IMODE((self.state_dir / "spotify_app.json").stat().st_mode), 0o600)

            save_spotify_token(cfg, {"sp_dc": "dc_secret"})
            save_spotify_token(cfg.token_file, {"refresh_token": "rt_secret", "access_token": "acc"})
            tok = json.loads(cfg.token_file.read_text())
            self.assertEqual(tok, {"sp_dc": "dc_secret", "refresh_token": "rt_secret", "access_token": "acc"})
            self.assertEqual(stat.S_IMODE(cfg.token_file.stat().st_mode), 0o600)

            cfg2 = HunterConfig.from_env()
            self.assertEqual(cfg2.interval, 600)
            self.assertTrue(cfg2.apply)
            self.assertTrue(cfg2.spotify_configured)

    def test_cache_hits_without_reparsing_until_file_changes(self):
        with self._env():
            cfg1 = HunterConfig.from_env()
            with mock.patch("hunter.config._read_json_dict", side_effect=AssertionError("should hit cache")):
                cfg2 = HunterConfig.from_env()
            self.assertIs(cfg1, cfg2)
            save_apply_override(cfg1, True)
            cfg3 = HunterConfig.from_env()
            self.assertTrue(cfg3.apply)

    def test_invalid_dl_byte_overrides_use_gb_hours_fallback(self):
        with self._env(
            HUNTER_DL_MAX_BUNDLE_BYTES="invalid",
            HUNTER_DL_KEEP_BYTES="bogus",
            HUNTER_DL_TTL_SECONDS="nope",
            HUNTER_DL_MAX_GB="12",
            HUNTER_DL_KEEP_GB="24",
            HUNTER_DL_TTL_HOURS="6",
        ):
            cfg = HunterConfig.from_env()
            self.assertEqual(cfg.dl_max_bundle_bytes, 12 * (1 << 30))
            self.assertEqual(cfg.dl_keep_bytes, 24 * (1 << 30))
            self.assertEqual(cfg.dl_ttl_seconds, 6 * 3600)


if __name__ == "__main__":
    unittest.main()

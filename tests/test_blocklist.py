"""Tests for hunter.blocklist (cfg path, snapshot immutability, no cache poisoning)."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import blocklist


class BlocklistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.auto = Path(self.tmp.name)
        (self.auto / "lib").mkdir(parents=True)
        self.cfg = SimpleNamespace(beets_automation_dir=self.auto)
        blocklist.invalidate()

    def tearDown(self):
        blocklist.invalidate()
        self.tmp.cleanup()

    def test_reads_cfg_path_and_matches_normalized_names(self):
        bl = self.auto / "lib" / "artist_blocklist.txt"
        bl.write_text("# header\nThe Beatles\nThe The\nGroße Straße\n", encoding="utf-8")

        snap = blocklist.snapshot(self.cfg)
        self.assertTrue(snap.available)
        self.assertEqual(snap.names, ("The Beatles", "The The", "Große Straße"))
        self.assertTrue(blocklist.is_blocked("Beatles", self.cfg))
        self.assertTrue(blocklist.is_blocked("The The", self.cfg))
        self.assertTrue(blocklist.is_blocked("Grosse Strasse", self.cfg))
        self.assertFalse(blocklist.is_blocked("Radiohead", self.cfg))
        self.assertEqual(
            blocklist.blocked_set(["Beatles", "Radiohead", "The The"], self.cfg),
            {"Beatles", "The The"},
        )

    def test_transient_oserror_does_not_poison_cache(self):
        bl = self.auto / "lib" / "artist_blocklist.txt"
        bl.write_text("Drake\n", encoding="utf-8")

        # First: transient OSError when no previous snapshot exists must NOT cache empty keys
        with mock.patch.object(Path, "read_text", side_effect=OSError("transient")):
            self.assertFalse(blocklist.available(self.cfg))
            self.assertFalse(blocklist.is_blocked("Drake", self.cfg))

        # Next call succeeds without needing invalidate()
        self.assertTrue(blocklist.available(self.cfg))
        self.assertTrue(blocklist.is_blocked("Drake", self.cfg))

        # Update file (change content/size/inode) + simulate transient read error:
        # retains previous valid snapshot instead of wiping keys!
        bl2 = self.auto / "lib" / "artist_blocklist.txt.new"
        bl2.write_text("Drake\nKanye\n", encoding="utf-8")
        os.replace(bl2, bl)
        with mock.patch.object(Path, "read_text", side_effect=OSError("transient")):
            self.assertTrue(blocklist.is_blocked("Drake", self.cfg))

        # Recovery picks up the updated file cleanly
        self.assertTrue(blocklist.is_blocked("Kanye", self.cfg))

    def test_env_fallback_when_cfg_is_none(self):
        bl = self.auto / "lib" / "artist_blocklist.txt"
        bl.write_text("Artist X\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"HUNTER_BEETS_AUTOMATION": str(self.auto)}):
            self.assertEqual(blocklist.path(), bl)
            self.assertTrue(blocklist.is_blocked("Artist X"))


if __name__ == "__main__":
    unittest.main()

"""Tests for hunter.domain (constants, normalization, tokens, paths, SQL, atomic I/O)
and hunter.lock."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hunter import domain, lock


class ConstantsTests(unittest.TestCase):
    def test_formats_and_extensions(self):
        self.assertIn("FLAC", domain.LOSSLESS_FORMATS)
        self.assertIn("DSF", domain.LOSSLESS_FORMATS)
        self.assertIn(".flac", domain.AUDIO_EXTENSIONS)
        self.assertIn(".m4p", domain.AUDIO_EXTENSIONS)
        self.assertIn(".opus", domain.AUDIO_EXTENSIONS)

    def test_priority_ordering(self):
        self.assertLess(domain.PRIORITY_URGENT, domain.PRIORITY_OPERATOR)
        self.assertLess(domain.PRIORITY_OPERATOR, domain.PRIORITY_DEFAULT)
        self.assertLess(domain.PRIORITY_MIN, domain.PRIORITY_URGENT)
        self.assertGreater(domain.PRIORITY_MAX, domain.PRIORITY_DEFAULT)


class NormalizationTests(unittest.TestCase):
    def test_normalize_diacritics_casefold_and_underscore(self):
        self.assertEqual(domain.normalize("Björk"), "bjork")
        self.assertEqual(domain.normalize("Große Straße"), "grosse strasse")
        self.assertEqual(domain.normalize("foo_bar-baz!"), "foo bar baz")
        self.assertEqual(domain.normalize(None), "")

    def test_norm_artist_the_the_and_feat(self):
        self.assertEqual(domain.norm_artist("The Beatles"), "beatles")
        self.assertEqual(domain.norm_artist("The The"), "the")
        self.assertEqual(domain.norm_artist("The"), "the")
        self.assertEqual(domain.norm_artist("Drake feat. Rihanna"), "drake")
        self.assertEqual(domain.norm_artist("Drake (ft. Rihanna)"), "drake")
        self.assertEqual(domain.norm_artist("Dance with the Dead"), "dance with the dead")

    def test_norm_album_preserves_live_and_strips_editions(self):
        self.assertEqual(domain.norm_album("Nevermind (Deluxe Edition)"), "nevermind")
        self.assertEqual(domain.norm_album("Nevermind [20th Anniversary]"), "nevermind")
        self.assertEqual(domain.norm_album("Abbey Road - 2019 Mix (Remastered)"), "abbey road 2019 mix")
        self.assertEqual(domain.norm_album("Abbey Road - 2011 Remastered"), "abbey road")
        self.assertEqual(domain.norm_album("Abbey Road - Deluxe Edition"), "abbey road")
        self.assertEqual(domain.norm_album("Unplugged (Live)"), "unplugged live")
        self.assertEqual(domain.norm_album("Unplugged (Live Version)"), "unplugged live version")
        self.assertEqual(domain.norm_album("Show [Live Edition]"), "show live edition")
        self.assertEqual(domain.norm_album("Live at Leeds (Deluxe Edition)"), "live at leeds")

    def test_blocklist_norm_parity(self):
        self.assertEqual(domain.blocklist_norm("The The"), "thethe")
        self.assertEqual(domain.blocklist_norm("The Beatles"), "beatles")
        self.assertEqual(domain.blocklist_norm("Große Straße"), "grossestrasse")
        self.assertEqual(domain.blocklist_norm("Five Finger Death Punch"), "5fingerdeathpunch")
        self.assertEqual(domain.blocklist_norm("Artist feat. Guest"), "artist")
        self.assertEqual(domain.blocklist_norm("東京事変"), "東京事変")


class TokenMatchingTests(unittest.TestCase):
    def test_extract_prefers_long_tokens_and_falls_back_for_short_names(self):
        self.assertEqual(domain.extract_match_tokens(domain.norm_artist("A Tribe Called Quest")), ["tribe", "called", "quest"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_artist("U2")), ["u2"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_artist("R.E.M.")), ["r", "e", "m"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_artist("The XX")), ["xx"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_artist("OK Go")), ["ok", "go"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_album("21")), ["21"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_album("IV")), ["iv"])
        self.assertEqual(domain.extract_match_tokens(domain.norm_album("X")), ["x"])
        self.assertEqual(domain.extract_match_tokens(""), [])

    def test_tokens_match_path_whole_token_and_initialism(self):
        self.assertFalse(domain.tokens_match_path([], "anything"))
        self.assertFalse(domain.tokens_match_path(["art"], "heartbreaker"))
        self.assertFalse(domain.tokens_match_path(["u2"], "youtube hits"))
        self.assertTrue(domain.tokens_match_path(["u2"], "u2 achtung baby"))
        self.assertTrue(domain.tokens_match_path(["r", "e", "m"], "rem automatic for the people", require_all=True))
        self.assertFalse(domain.tokens_match_path(["r", "e", "m"], "remember me", require_all=True))
        self.assertTrue(domain.tokens_match_path(["ok", "go"], "ok go oh no", require_all=True))
        self.assertFalse(domain.tokens_match_path(["ok", "go"], "ok computer", require_all=True))


class PathAndSqlTests(unittest.TestCase):
    def test_resolve_under_and_container_to_host_path(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "merged"
            q = Path(d) / "quarantine"
            outside = Path(d) / "secret.txt"
            outside.write_text("nope")
            (root / "Artist").mkdir(parents=True)
            track = root / "Artist" / "01.flac"
            track.write_bytes(b"fLaC")
            evil_link = root / "evil"
            evil_link.symlink_to(outside)
            q.mkdir()
            qfile = q / "bad.mp3"
            qfile.write_bytes(b"mp3")

            cfg = SimpleNamespace(music_root=root, quarantine_dir=q)
            self.assertEqual(domain.container_to_host_path(cfg, "/merged/Artist/01.flac"), track.resolve())
            self.assertEqual(domain.container_to_host_path(cfg, b"/merged/Artist/01.flac"), track.resolve())
            self.assertIsNone(domain.container_to_host_path(cfg, "/merged/../../etc/passwd"))
            self.assertIsNone(domain.container_to_host_path(cfg, "/merged/evil"))
            self.assertIsNone(domain.container_to_host_path(cfg, "/etc/passwd"))
            self.assertIsNone(domain.container_to_host_path(cfg, "/merged/a\x00b"))
            self.assertIsNone(domain.container_to_host_path(cfg, "/quarantine/bad.mp3"))
            self.assertEqual(
                domain.container_to_host_path(cfg, "/quarantine/bad.mp3", allow_quarantine=True),
                qfile.resolve(),
            )
            self.assertIsNone(domain.container_to_host_path(cfg, "/mergedx/01.flac"))

            cfg_custom = SimpleNamespace(
                music_root=root,
                quarantine_dir=q,
                beets_music_root="/library/music",
                beets_quarantine_root="/library/quarantine",
            )
            self.assertEqual(
                domain.container_to_host_path(cfg_custom, "/library/music/Artist/01.flac"),
                track.resolve(),
            )
            self.assertIsNone(
                domain.container_to_host_path(cfg_custom, "/merged/Artist/01.flac")
            )
            self.assertEqual(
                domain.container_to_host_path(
                    cfg_custom, "/library/quarantine/bad.mp3", allow_quarantine=True
                ),
                qfile.resolve(),
            )

    def test_escape_like_and_chunked(self):
        self.assertEqual(domain.escape_like(r"a\b%c_d"), r"a\\b\%c\_d")
        self.assertEqual(list(domain.chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])
        with self.assertRaises(ValueError):
            list(domain.chunked([1], 0))

    def test_atomic_writes_and_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "secret.json"
            domain.atomic_write_json(p, {"token": "abc"}, mode=0o600)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(json.loads(p.read_text()), {"token": "abc"})
            domain.atomic_write_text(p, "hello")
            self.assertEqual(p.read_text(), "hello")
            # No leftover temp files
            self.assertEqual([x.name for x in p.parent.iterdir()], ["secret.json"])


class LockTests(unittest.TestCase):
    def test_hold_writes_metadata_does_not_pretruncate_and_probes_is_locked(self):
        with tempfile.TemporaryDirectory() as d:
            lf = Path(d) / "pass.lock"
            lf.write_text("pid=99999 ts=1 cmd=previous\n")
            self.assertFalse(lock.is_locked(lf))
            with lock.hold(lf):
                self.assertTrue(lock.is_locked(lf))
                holder = lock.read_holder(lf)
                self.assertIn(f"pid={os.getpid()}", holder)
                with self.assertRaises(lock.LockBusy):
                    with lock.hold(lf):
                        pass
                # Failed second hold must NOT have truncated the lockfile!
                self.assertIn(f"pid={os.getpid()}", lock.read_holder(lf))
            self.assertFalse(lock.is_locked(lf))


if __name__ == "__main__":
    unittest.main()

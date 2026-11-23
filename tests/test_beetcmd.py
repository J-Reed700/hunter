from __future__ import annotations

import unittest

from hunter import beetcmd


class BeetCmdTests(unittest.TestCase):
    def test_positional_rejects_leading_dash_bypass(self) -> None:
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build("duplicates", positional=["--delete"])
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build("modify", query="artist:Foo", positional=["-d"])

    def test_duplicates_checksum_flag_removed_and_blocked(self) -> None:
        dup = beetcmd.resolve("duplicates")
        self.assertIsNotNone(dup)
        self.assertNotIn("-C", {f.flag for f in dup.flags})
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build("duplicates", options={"-C": "id"})
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("duplicates -C 'sh -c id'")
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("duplicates --checksum=id")

    def test_extractart_is_mutate_and_blocks_traversal_and_abs_paths(self) -> None:
        cmd = beetcmd.resolve("extractart")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.cls, "mutate")
        for bad in ("/etc/passwd", "../escape.jpg", "cover.jpg", "config/foo", "/merged/../etc/x"):
            with self.assertRaises(beetcmd.PolicyError):
                beetcmd.build("extractart", query="artist:Foo", options={"-o": bad}, apply=True)
            with self.assertRaises(beetcmd.PolicyError):
                beetcmd.build("embedart", query="artist:Foo", options={"-f": bad}, apply=True)
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build("extractart", query="artist:Foo", options={"-n": "sub/cover.jpg"}, apply=True)
        argv = beetcmd.build(
            "extractart",
            query="artist:Foo",
            options={"-o": "/merged/Music/Foo/cover.jpg", "-n": "cover.jpg"},
            apply=True,
        )
        self.assertEqual(
            argv,
            ["extractart", "-o", "/merged/Music/Foo/cover.jpg", "-n", "cover.jpg", "artist:Foo"],
        )

    def test_dry_run_injects_pretend_or_rejects_unpretendable_mutate(self) -> None:
        self.assertEqual(beetcmd.build("move", query="artist:Foo", apply=False), ["move", "-p", "artist:Foo"])
        self.assertEqual(beetcmd.build_raw("write artist:Foo", apply=False), ["write", "-p", "artist:Foo"])
        self.assertEqual(beetcmd.build_raw("convert artist:Foo", apply=False), ["convert", "-p", "artist:Foo"])
        for cmd_name in ("scrub", "ftintitle", "lastgenre", "replaygain", "fingerprint", "submit", "fetchart", "extractart"):
            with self.assertRaises(beetcmd.PolicyError):
                beetcmd.build(cmd_name, query="artist:Foo", apply=False)
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("scrub", apply=False)

        cat = {c["name"]: c for c in beetcmd.catalogue()}
        self.assertFalse(cat["scrub"]["can_dry_run"])
        self.assertTrue(cat["write"]["can_dry_run"])
        self.assertTrue(cat["modify"]["can_dry_run"])
        self.assertTrue(cat["list"]["can_dry_run"])

        # album_native commands return album=True in query_of even without -a
        _, _, alb = beetcmd.query_of(["fetchart", "artist:Foo"])
        self.assertTrue(alb)

    def test_rejects_empty_token_and_equals_on_boolean_flag(self) -> None:
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("modify -a '' year=2000", apply=False)
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("modify -a=bogus artist:Foo year=2000", apply=False)

    def test_for_preview_allows_non_dry_run_commands_and_preserves_needs_confirm(self) -> None:
        argv_fetch = beetcmd.build("fetchart", query="artist:Foo", apply=False, for_preview=True)
        self.assertEqual(argv_fetch, ["fetchart", "artist:Foo"])
        argv_scrub = beetcmd.build_raw("scrub artist:Foo", apply=False, for_preview=True)
        self.assertEqual(argv_scrub, ["scrub", "artist:Foo"])

        # Empty-query move preview omits -p so needs_confirm returns True
        argv_move = beetcmd.build("move", query="", apply=False, for_preview=True)
        self.assertEqual(argv_move, ["move"])
        cmd, qtoks, _ = beetcmd.query_of(argv_move)
        self.assertTrue(beetcmd.needs_confirm(cmd, qtoks, 10, argv_move))

    def test_rejects_confirm_in_options_when_dry_run_and_equals_smuggling(self) -> None:
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build("modify", query="artist:Foo", options={"-y": True},
                          positional=["genre=Jazz"], apply=False)
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("modify -y artist:Foo genre=Jazz", apply=False)
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build("list", options={"-f=/etc/passwd": True})

    def test_query_none_enforced_in_build_raw(self) -> None:
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("fields artist:Foo")
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build_raw("version extra")
        # help accepts 1 positional subcommand name
        self.assertEqual(beetcmd.build_raw("help modify"), ["help", "modify"])

    def test_modify_keeps_colon_queries_with_bang_or_equals(self) -> None:
        argv = beetcmd.build_raw("modify -a artist:Wham! album:Panic! title:E=MC2 year=1984", apply=True)
        self.assertEqual(
            argv,
            ["modify", "-a", "artist:Wham!", "album:Panic!", "title:E=MC2", "year=1984", "-y"],
        )
        cmd, qtoks, album = beetcmd.query_of(argv)
        self.assertEqual(cmd.name, "modify")
        self.assertTrue(album)
        self.assertEqual(qtoks, ["artist:Wham!", "album:Panic!", "title:E=MC2"])

    def test_beet_argv_and_custom_allowed_roots(self) -> None:
        from types import SimpleNamespace

        self.assertEqual(beetcmd.BEET_ARGV, ["beet"])
        self.assertEqual(beetcmd.beet_argv(), ["beet"])
        cfg = SimpleNamespace(beets_cli=("/opt/venv/bin/python", "/srv/beet_wrap.py"))
        self.assertEqual(beetcmd.beet_argv(cfg), ["/opt/venv/bin/python", "/srv/beet_wrap.py"])

        argv = beetcmd.build(
            "extractart",
            query="artist:Foo",
            options={"-o": "/data/music/Foo/cover.jpg"},
            apply=True,
            allowed_roots=("/data/music", "/data/quarantine"),
        )
        self.assertEqual(argv, ["extractart", "-o", "/data/music/Foo/cover.jpg", "artist:Foo"])
        with self.assertRaises(beetcmd.PolicyError):
            beetcmd.build(
                "extractart",
                query="artist:Foo",
                options={"-o": "/merged/Foo/cover.jpg"},
                apply=True,
                allowed_roots=("/data/music", "/data/quarantine"),
            )


if __name__ == "__main__":
    unittest.main()

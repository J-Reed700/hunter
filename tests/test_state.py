"""Tests for hunter.state (Want lifecycle, transactions, LIKE escaping, saga, Spotify cache)."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hunter import blocklist
from hunter.db import reset_schema_cache
from hunter.state import HunterState, Want


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hunter.db"
        self.auto = Path(self.tmp.name) / "auto"
        (self.auto / "lib").mkdir(parents=True)
        reset_schema_cache()
        blocklist.invalidate()
        self.st = HunterState(self.db_path)

    def tearDown(self):
        self.st.close()
        reset_schema_cache()
        blocklist.invalidate()
        self.tmp.cleanup()

    def test_upsert_want_resurrect_and_blocklist_safety(self):
        bl = self.auto / "lib" / "artist_blocklist.txt"
        bl.write_text("Blocked Artist\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"HUNTER_BEETS_AUTOMATION": str(self.auto)}):
            blocklist.invalidate()
            self.assertFalse(self.st.upsert_want("k_blk", "Blocked Artist", "Alb", 2020, 10, "manual"))
            self.assertIsNone(self.st.want_by_key("k_blk"))

            # sqlite3.Error during has_want/log_event must NOT be swallowed into creating blocked want
            with mock.patch.object(self.st, "has_want", side_effect=sqlite3.OperationalError("boom")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.st.upsert_want("k_blk2", "Blocked Artist", "Alb", 2020, 10, "manual")
            self.assertIsNone(self.st.want_by_key("k_blk2"))

        # Normal insert, grab, skip, then resurrect clears stale grab metadata
        self.assertTrue(self.st.upsert_want("k1", "Radiohead", "OK Computer", 1997, 12, "saved_albums"))
        w = self.st.want_by_key("k1")
        self.assertIsInstance(w, Want)
        assert w is not None
        self.st.mark_grabbed(w.id, "peer1", "/dir/1")
        self.st.open_drop_window(w.id, 3600)
        self.st.set_status(w.id, "skipped", "parked")
        self.assertTrue(self.st.upsert_want("k1", "Radiohead", "OK Computer", 1997, 12, "request", resurrect=True))
        w2 = self.st.want_by_key("k1")
        assert w2 is not None
        self.assertEqual(w2.status, "wanted")
        self.assertIsNone(w2.grabbed_ts)
        self.assertIsNone(w2.grabbed_username)
        self.assertIsNone(w2.grabbed_dir)
        self.assertEqual(w2.drop_window_until, 0)

    def test_park_restore_preserves_prior_status_including_exhausted(self):
        self.st.upsert_want("w1", "Drake", "Take Care", 2011, 18, "saved_albums")
        self.st.upsert_want("w2", "Drake", "Views", 2016, 20, "saved_albums")
        id1 = self.st.want_id_by_key("w1")
        id2 = self.st.want_id_by_key("w2")
        assert id1 and id2
        self.st.set_status(id2, "exhausted", "3 tries")

        self.assertEqual(self.st.park_wants(["Drake"], [], "job-X"), 2)
        self.assertEqual(self.st.get_want(id1).status, "skipped")  # type: ignore[union-attr]
        self.assertEqual(self.st.get_want(id2).status, "skipped")  # type: ignore[union-attr]

        self.assertEqual(self.st.restore_parked("job-X"), 2)
        self.assertEqual(self.st.get_want(id1).status, "wanted")     # type: ignore[union-attr]
        self.assertEqual(self.st.get_want(id2).status, "exhausted")  # type: ignore[union-attr]

    def test_drop_wants_for_blocked_and_like_escaping(self):
        self.st.upsert_want("a1", "Drake_", "Alb", 2020, 10, "manual")
        self.st.upsert_want("a2", "Drake", "Alb2", 2020, 10, "manual")
        self.st.upsert_want("a3", "100% Pure", "Hit_1", 2020, 10, "manual")

        # Literal drop_wants does not treat _ or % as wildcards
        self.assertEqual(self.st.drop_wants("Drake_", "Alb", literal=True), 1)
        self.assertEqual(self.st.want_by_key("a2").status, "wanted")  # type: ignore[union-attr]

        # wants_query & wants_count_filtered escape LIKE wildcards
        rows = self.st.wants_query(statuses=[], source="", text="100%", sort="updated", limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["artist"], "100% Pure")
        self.assertEqual(self.st.wants_count_filtered(text="100%"), 1)

        # drop_wants_for_blocked matches domain.blocklist_norm
        self.assertEqual(self.st.drop_wants_for_blocked({"drake"}, "blocked"), 1)
        self.assertEqual(self.st.want_by_key("a2").status, "skipped")  # type: ignore[union-attr]

    def test_transaction_batches_and_rolls_back(self):
        with self.st.transaction():
            self.st.upsert_want("t1", "A", "B", 2020, 5, "manual")
            self.st.upsert_want("t2", "C", "D", 2021, 5, "manual")
        self.assertTrue(self.st.has_want("t1"))
        self.assertTrue(self.st.has_want("t2"))

        with self.assertRaises(RuntimeError):
            with self.st.transaction():
                self.st.upsert_want("t3", "E", "F", 2022, 5, "manual")
                raise RuntimeError("abort")
        self.assertFalse(self.st.has_want("t3"))

    def test_record_pass_keeps_valid_json_and_peer_guards_prowlarr(self):
        huge = json.dumps({"ok": True, "big": ["x" * 500 for _ in range(30)]})
        self.st.record_pass(100, 1.2, True, True, huge)
        parsed = self.st.recent_passes_parsed(1)
        self.assertEqual(len(parsed), 1)
        self.assertTrue(parsed[0]["stats"]["ok"])
        self.assertTrue(parsed[0]["stats"]["_truncated"])

        self.st.record_peer_grab("prowlarr:nzbgeek")
        self.st.record_peer_outcome("prowlarr:nzbgeek", True)
        self.st.record_peer_outcome("bad_peer", False)
        peers = {r["username"]: dict(r) for r in self.st.peer_stat_rows()}
        self.assertNotIn("prowlarr:nzbgeek", peers)
        self.assertIn("bad_peer", peers)

    def test_spotify_cache_orphan_prune_and_match_ttl(self):
        self.st.upsert_playlist("pl1", "Mix", 1)
        self.st.sp_upsert_track("tr1", "T1", "Art", "Alb", "aid1", 2020, 200)
        self.st.sp_upsert_track("tr2", "T2", "Art", "Alb", "aid1", 2020, 200)
        self.st.sp_link_track("pl1", "tr1", 0, gen=1)
        self.st.sp_link_track("pl1", "tr2", 1, gen=1)
        self.st.sp_playlist_done("pl1", gen=1, track_count=2)
        self.assertEqual(self.st.sp_counts()["tracks"], 2)

        # Next crawl drops tr2 -> tr2 pruned from sp_tracks
        self.st.sp_link_track("pl1", "tr1", 0, gen=2)
        self.st.sp_playlist_done("pl1", gen=2, track_count=1)
        self.assertEqual(self.st.sp_counts()["tracks"], 1)

        # Track match hit TTL & invalidation
        self.st.set_track_match("tr1", "song-99")
        self.assertEqual(self.st.get_track_match("tr1"), (True, "song-99"))
        self.st._conn.execute("UPDATE track_matches SET matched_ts=1 WHERE spotify_track_id='tr1'")
        self.st._conn.commit()
        self.assertEqual(self.st.get_track_match("tr1", hit_ttl_s=3600), (False, None))
        self.st.set_track_match("tr1", "song-100")
        self.st.invalidate_track_match("tr1")
        self.assertEqual(self.st.get_track_match("tr1"), (False, None))

    def test_public_repository_methods_replace_raw_conn(self):
        self.st.upsert_want("m1", "Art", "Alb", 2020, 10, "saved_albums")
        wid = self.st.want_id_by_key("m1")
        assert wid is not None
        self.assertTrue(self.st.source_active("saved_albums"))
        self.assertTrue(self.st.update_want_fields(wid, artist="Art2", track_count=12))
        self.assertEqual(self.st.want_row(wid)["artist"], "Art2")  # type: ignore[index]
        self.assertEqual(self.st.bulk_set_priority([(wid, -50)]), 1)
        self.assertEqual(self.st.want_by_key("m1").priority, -50)  # type: ignore[union-attr]
        self.assertEqual(self.st.spotify_want_counts()["wanted"], 1)
        self.assertEqual(self.st.wants_status_map(["m1"])["m1"]["status"], "wanted")

    def test_drop_wants_literal_wildcards_and_legacy_saved_counts(self):
        self.st.upsert_want("legacy:1", "A", "B", 2020, 10, "saved")
        self.st.upsert_want("gap:1", "100%_Artist", "100%_Album", 2020, 10, "gapfill")
        self.st.upsert_want("gap:2", "Other", "OtherAlb", 2020, 10, "gapfill")

        # Counts include legacy 'saved' alongside 'saved_albums' / 'playlist:*'
        self.assertEqual(self.st.spotify_want_counts().get("wanted"), 1)
        # wants_query exposes spotify_album_id for dashboard cover art
        rows = self.st.wants_query(statuses=["wanted"], source="saved", text="", sort="updated", limit=10)
        self.assertEqual(rows[0]["spotify_album_id"], "legacy:1")

        # Literal match only drops matching literal underscores/percents
        self.assertEqual(self.st.drop_wants("100%_Artist", "100%_Album", "gapfill", literal=True), 1)
        self.assertEqual(self.st.want_by_key("gap:2").status, "wanted")  # type: ignore[union-attr]
        # Wildcard '%' for artist/album in literal=True clears the whole source
        self.assertEqual(self.st.drop_wants("%", "%", "gapfill", literal=True), 1)
        self.assertEqual(self.st.want_by_key("gap:2").status, "skipped")  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()

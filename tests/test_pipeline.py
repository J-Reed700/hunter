"""Unit tests for hunter.pipeline."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hunter import mb, pipeline
from hunter.config import HunterConfig
from hunter.db import reset_schema_cache
from hunter.library import BeetsLibrary
from hunter.prowlarr import ProwlarrClient, ProwlarrError, Release
from hunter.slskd_client import SlskdClient, SlskdError
from hunter.state import HunterState


def _make_cfg(tmp: Path, *, apply: bool = True) -> HunterConfig:
    return HunterConfig(
        spotify_client_id="",
        spotify_client_secret="",
        token_file=tmp / "spotify_token.json",
        want_sources=("saved_albums", "playlists"),
        playlist_exclude="",
        slskd_url="http://slskd:5030",
        slskd_api_key="key",
        search_timeout=15,
        max_grabs_per_pass=2,
        min_candidate_ratio=0.8,
        max_candidate_ratio=2.5,
        prefer_lossless=True,
        min_bitrate=192,
        owned_coverage=0.8,
        max_attempts=3,
        search_miss_cap=5,
        peer_ban_strikes=3,
        drop_window_hours=48,
        backoff_seconds=(60, 120),
        grab_stale_seconds=3600,
        prowlarr_url="http://prowlarr:9696",
        prowlarr_api_key="pkey",
        prowlarr_fallback=True,
        prowlarr_after_misses=2,
        sync_playlists=False,
        playlist_match_cap=100,
        playlist_prefix="",
        navidrome_url="http://nd:4533",
        navidrome_user="",
        navidrome_pass="",
        webhook_url="http://ntfy/test",
        notify_min_interval=1800,
        artist_check_hours=24,
        ui_port=0,
        ui_token="",
        apply=apply,
        interval=1800,
        beets_db=tmp / "musiclibrary.db",
        navidrome_db=tmp / "navidrome.db",
        audiomuse_url="",
        audiomuse_token="",
        quarantine_dir=tmp / "quarantine",
        state_dir=tmp,
        state_db=tmp / "hunter.db",
        log_dir=tmp / "logs",
        log_keep_days=7,
        pass_deadline=600,
        docker_sock="",
        beets_container="beets",
        beets_exec_user="abc",
        beets_automation_dir=tmp / "auto",
        music_root=tmp / "merged",
        storage_fstype=None,
    )


def _create_beets_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE albums (
            id INTEGER PRIMARY KEY,
            albumartist TEXT,
            album TEXT,
            year INTEGER,
            mb_releasegroupid TEXT
        );
        CREATE TABLE items (
            id INTEGER PRIMARY KEY,
            album_id INTEGER,
            disc INTEGER,
            disctotal INTEGER DEFAULT 1,
            tracktotal INTEGER,
            format TEXT,
            path BLOB
        );
        """
    )
    conn.commit()
    conn.close()


class TestPipelineQueriesAndWeights(unittest.TestCase):
    def tearDown(self) -> None:
        reset_schema_cache()

    def test_gap_rows_multidisc_and_lossy_rows_dsf(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            _create_beets_db(cfg.beets_db)
            conn = sqlite3.connect(cfg.beets_db)
            conn.executescript(
                """
                INSERT INTO albums VALUES (1, 'Artist A', 'Double Alb', 2020, 'rg1');
                -- Disc 1: tracktotal 5 (4 present), Disc 2: tracktotal 5 (2 present) -> 6/10 = 0.6 < 0.8
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (1, 1, 1, 5, 'FLAC', '/merged/a/1.flac');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (2, 1, 1, 5, 'FLAC', '/merged/a/2.flac');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (3, 1, 1, 5, 'FLAC', '/merged/a/3.flac');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (4, 1, 1, 5, 'FLAC', '/merged/a/4.flac');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (5, 1, 2, 5, 'FLAC', '/merged/a/5.flac');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (6, 1, 2, 5, 'FLAC', '/merged/a/6.flac');

                INSERT INTO albums VALUES (2, 'Artist B', 'SACD Alb', 2021, 'rg2');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (7, 2, 1, 4, 'DSF', '/merged/b/1.dsf');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (8, 2, 1, 4, 'DSF', '/merged/b/2.dsf');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (9, 2, 1, 4, 'DSF', '/merged/b/3.dsf');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (10, 2, 1, 4, 'DSF', '/merged/b/4.dsf');

                INSERT INTO albums VALUES (3, 'Artist C', 'MP3 Alb', 2022, 'rg3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (11, 3, 1, 4, 'MP3', '/merged/c/1.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (12, 3, 1, 4, 'MP3', '/merged/c/2.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (13, 3, 1, 4, 'MP3', '/merged/c/3.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (14, 3, 1, 4, 'MP3', '/merged/c/4.mp3');
                """
            )
            conn.commit()
            conn.close()

            gaps = pipeline.gap_rows(cfg)
            self.assertEqual(len(gaps), 1)
            self.assertEqual(gaps[0][0], 1)
            self.assertEqual(gaps[0][4], 6)
            self.assertEqual(gaps[0][5], 10)

            lossy = pipeline.lossy_rows(cfg)
            self.assertEqual([r[0] for r in lossy], [3])  # DSF is lossless

    def test_auto_reseed_respects_source_active_and_play_weights(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            _create_beets_db(cfg.beets_db)
            conn = sqlite3.connect(cfg.beets_db)
            conn.executescript(
                """
                INSERT INTO albums VALUES (10, 'Art', 'Lossy', 2020, '');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (1, 10, 1, 4, 'MP3', '/merged/1.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (2, 10, 1, 4, 'MP3', '/merged/2.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (3, 10, 1, 4, 'MP3', '/merged/3.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (4, 10, 1, 4, 'MP3', '/merged/4.mp3');
                """
            )
            conn.commit()
            conn.close()

            # Navidrome DB with duplicate album rows
            nd = sqlite3.connect(cfg.navidrome_db)
            nd.executescript(
                """
                CREATE TABLE album (id TEXT, name TEXT, album_artist TEXT);
                CREATE TABLE annotation (item_id TEXT, item_type TEXT, play_count INTEGER);
                INSERT INTO album VALUES ('a1', 'Alb1', 'Band'), ('a2', 'Alb1 (Deluxe)', 'Band');
                INSERT INTO annotation VALUES ('a1', 'album', 5), ('a2', 'album', 7);
                """
            )
            nd.commit()
            nd.close()

            with HunterState(cfg.state_db) as st:
                # Paused upgrade program (all skipped) -> auto_reseed must NOT reseed it
                st.upsert_want("upgrade:beets:99", "Old", "OldAlb", 2019, 4, "upgrade")
                st.set_source_status("upgrade", "wanted", "skipped")
                reseeded = pipeline.auto_reseed(cfg, st)
                self.assertEqual(reseeded, {})

                # Play weights: sum duplicates (5 + 7 = 12), update continuously (-5 -> -12),
                # include saved_albums, protect operator (-50) & non-backlog sources
                st.upsert_want("gapfill:beets:1", "Band", "Alb1", 2020, 8, "gapfill")
                st.set_priority("gapfill:beets:1", -5)  # already weighted once -> still updates to -12
                st.upsert_want("saved:1", "Band", "Alb1", 2020, 8, "saved_albums")
                st.upsert_want("gapfill:beets:2", "Band", "Alb1", 2020, 8, "gapfill")
                st.set_priority("gapfill:beets:2", -50)  # operator priority preserved
                st.upsert_want("manual:band:alb1", "Band", "Alb1", 2020, 8, "manual")
                changed = pipeline.apply_play_weights(cfg, st)
                self.assertEqual(changed, 2)
                self.assertEqual(st.want_by_key("gapfill:beets:1").priority, -12)
                self.assertEqual(st.want_by_key("saved:1").priority, -12)
                self.assertEqual(st.want_by_key("gapfill:beets:2").priority, -50)
                self.assertEqual(st.want_by_key("manual:band:alb1").priority, 0)


class TestReconcileHuntWatch(unittest.TestCase):
    def tearDown(self) -> None:
        reset_schema_cache()

    def test_reconcile_recovery_disk_check_upgrade_and_fast_fail(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            cfg.music_root.mkdir(parents=True)
            _create_beets_db(cfg.beets_db)
            conn = sqlite3.connect(cfg.beets_db)
            conn.executescript(
                """
                INSERT INTO albums VALUES (1, 'RecArt', 'MissingAlb', 2020, '');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (1, 1, 1, 1, 'FLAC', '/merged/RecArt/missing.flac');

                INSERT INTO albums VALUES (2, 'RecArt2', 'RestoredAlb', 2020, '');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (2, 2, 1, 1, 'FLAC', '/merged/RecArt2/present.flac');

                INSERT INTO albums VALUES (3, 'UpgArt', 'LossyAlb', 2020, '');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (3, 3, 1, 2, 'MP3', '/merged/UpgArt/1.mp3');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (4, 3, 1, 2, 'MP3', '/merged/UpgArt/2.mp3');
                """
            )
            conn.commit()
            conn.close()
            (cfg.music_root / "RecArt2").mkdir()
            (cfg.music_root / "RecArt2" / "present.flac").write_bytes(b"flac")

            lib = BeetsLibrary(cfg.beets_db)
            slskd = MagicMock(spec=SlskdClient)
            slskd.download_states.return_value = {
                ("peer_partial", "dir/partial"): {
                    "total": 10,
                    "succeeded": 2,
                    "errored": 8,
                    "active": 0,
                    "pct": 20.0,
                }
            }
            slskd.cancel_dir.return_value = 1

            with HunterState(cfg.state_db) as st:
                st.upsert_want("rec:1", "RecArt", "MissingAlb", 2020, 1, "recovery")
                st.upsert_want("rec:2", "RecArt2", "RestoredAlb", 2020, 1, "recovery")
                st.upsert_want("upgrade:beets:999", "Gone", "DeletedAlb", 2020, 4, "upgrade")
                st.upsert_want("req:fastfail", "Band", "Partial", 2020, 10, "request")
                wid_ff = st.want_id_by_key("req:fastfail")
                st.mark_grabbed(wid_ff, "peer_partial", r"dir\partial")

                stats = pipeline.reconcile(cfg, st, lib, slskd, now=time.time())
                # Missing recovery stays wanted; restored recovery & deleted upgrade -> done; partial-error -> failed_fast
                self.assertEqual(st.want_by_key("rec:1").status, "wanted")
                self.assertEqual(st.want_by_key("rec:2").status, "done")
                self.assertEqual(st.want_by_key("upgrade:beets:999").status, "done")
                self.assertEqual(st.want_by_key("req:fastfail").status, "wanted")
                self.assertEqual(stats["failed_fast"], 1)
                self.assertEqual(stats["done"], 2)

    def test_hunt_bare_album_gate_enqueue_miss_and_prowlarr_tried(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp, apply=True)
            slskd = MagicMock(spec=SlskdClient)
            prowlarr = MagicMock(spec=ProwlarrClient)

            with HunterState(cfg.state_db) as st:
                # Want 1: enqueue fails -> search_misses MUST stay 0!
                st.upsert_want("req:u2", "U2", "War", 1983, 2, "request")
                # Want 2: Prowlarr fallback after misses (two distinct has_artist tiers)
                st.upsert_want("req:pw", "The Air", "Moon Safari", 1998, 2, "request")
                wid_pw = st.want_id_by_key("req:pw")
                st.bump_backoff(wid_pw, -100, count_attempt=True)

                def slskd_search(text, timeout):
                    if "War" in text:
                        return [
                            {
                                "username": "p1",
                                "hasFreeUploadSlot": True,
                                "files": [
                                    {"filename": "U2/War/01.flac", "size": 10_000_000},
                                    {"filename": "U2/War/02.flac", "size": 10_000_000},
                                ],
                            }
                        ]
                    return []

                slskd.search.side_effect = slskd_search
                slskd.enqueue.side_effect = SlskdError("peer offline")

                rel1 = Release("Air - Moon Safari FLAC", "g1", 1, "RED", "torrent", 100_000_000, 10, True, 1.0)
                prowlarr.search.side_effect = [ProwlarrError("tier1 hiccup"), [rel1]]
                prowlarr.grab.side_effect = ProwlarrError("grab failed")

                stats = pipeline.hunt(cfg, st, slskd, prowlarr, True, 2, time.time() + 60)
                self.assertEqual(stats["enqueue_error"], 1)
                self.assertEqual(st.want_by_key("req:u2").search_misses, 0)
                # Tier 2 ran after tier 1 error, tried rel1 and failed -> recorded in attempts
                self.assertIn(("prowlarr:RED", rel1.title), st.tried_candidates(wid_pw))

    def test_watch_artists_dates_secondary_types_and_owned_gate(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            _create_beets_db(cfg.beets_db)
            conn = sqlite3.connect(cfg.beets_db)
            conn.executescript(
                """
                INSERT INTO albums VALUES (1, 'Band', 'AlreadyOwned', 2026, 'rg-owned');
                INSERT INTO items (id, album_id, disc, tracktotal, format, path) VALUES (1, 1, 1, 2, 'FLAC', '/m/1.flac'),
                                         (2, 1, 1, 2, 'FLAC', '/m/2.flac');
                """
            )
            conn.commit()
            conn.close()
            lib = BeetsLibrary(cfg.beets_db)

            followed_ts = int(time.mktime(time.strptime("2026-06-01", "%Y-%m-%d")))
            today = time.strftime("%Y-%m-%d")

            with HunterState(cfg.state_db) as st:
                st.follow_artist("mbid-1", "Band")
                st._conn.execute(
                    "UPDATE artists SET followed_ts=?, last_check_ts=0, want_types='album,ep,live'",
                    (followed_ts,),
                )
                st._conn.commit()

                rgs = [
                    # Year-only "2026" must NOT match mid-2026 follow date
                    {"rgid": "rg-yearonly", "album": "YearOnly", "year": 2026, "date": "2026", "type": "Album", "secondary": []},
                    # Live + Compilation must be rejected
                    {"rgid": "rg-livecomp", "album": "LiveComp", "year": 2026, "date": today, "type": "Album", "secondary": ["Live", "Compilation"]},
                    # Live Single must be rejected when Single not in want_types
                    {"rgid": "rg-livesingle", "album": "LiveSingle", "year": 2026, "date": today, "type": "Single", "secondary": ["Live"]},
                    # Already owned must be skipped
                    {"rgid": "rg-owned", "album": "AlreadyOwned", "year": 2026, "date": today, "type": "Album", "secondary": []},
                    # Real new drop (survives MBUnavailable on rg_track_count!)
                    {"rgid": "rg-new", "album": "Brand New", "year": 2026, "date": today, "type": "Album", "secondary": []},
                ]
                with patch.object(mb, "artist_release_groups", return_value=rgs), \
                     patch.object(mb, "rg_track_count", side_effect=mb.MBUnavailable("503")):
                    stats = pipeline.watch_artists(cfg, st, lib, time.time() + 60)

                self.assertEqual(stats["new_wants"], 1)
                self.assertIsNotNone(st.want_by_key("release:rg:rg-new"))
                self.assertIsNone(st.want_by_key("release:rg:rg-yearonly"))
                self.assertIsNone(st.want_by_key("release:rg:rg-livecomp"))
                self.assertIsNone(st.want_by_key("release:rg:rg-livesingle"))
                self.assertIsNone(st.want_by_key("release:rg:rg-owned"))

    def test_run_pass_heartbeat_storage_failure_and_notify(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp, apply=False)
            cfg.music_root.mkdir(parents=True)
            cfg.quarantine_dir.mkdir(parents=True)
            _create_beets_db(cfg.beets_db)

            # Shadow mode with would_grab must NOT fire ntfy notification
            with patch("hunter.pipeline.SlskdClient") as mock_slskd_cls, \
                 patch("hunter.pipeline.Notifier.send") as mock_notify:
                slskd = mock_slskd_cls.return_value.__enter__.return_value
                slskd.download_states.return_value = {}
                rc = pipeline.run_pass(cfg, apply=False)
                self.assertEqual(rc, 0)
                mock_notify.assert_not_called()
                hb = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
                self.assertEqual(hb["status"], "ok")

            # Missing music_root -> storage failure returns non-zero rc=1
            cfg.music_root.rmdir()
            with patch("hunter.pipeline.SlskdClient") as mock_slskd_cls:
                slskd = mock_slskd_cls.return_value.__enter__.return_value
                slskd.download_states.return_value = {}
                rc_bad = pipeline.run_pass(cfg, apply=False)
                self.assertEqual(rc_bad, 1)

            # Restored music_root, but slskd suffers total outage (transport_error >= 3) -> rc=1
            cfg.music_root.mkdir()
            with patch("hunter.pipeline.SlskdClient") as mock_slskd_cls, \
                 patch("hunter.pipeline.hunt", return_value={"searched": 3, "grabbed": 0, "prowlarr": 0, "transport_error": 3}):
                slskd = mock_slskd_cls.return_value.__enter__.return_value
                slskd.download_states.return_value = {}
                rc_slskd_down = pipeline.run_pass(cfg, apply=False)
                self.assertEqual(rc_slskd_down, 1)

            # Unreadable beets DB -> rc=1
            cfg.beets_db.unlink()
            with patch("hunter.pipeline.SlskdClient") as mock_slskd_cls:
                slskd = mock_slskd_cls.return_value.__enter__.return_value
                slskd.download_states.return_value = {}
                rc_beets_down = pipeline.run_pass(cfg, apply=False)
                self.assertEqual(rc_beets_down, 1)


if __name__ == "__main__":
    unittest.main()

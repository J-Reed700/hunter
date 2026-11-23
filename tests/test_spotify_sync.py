"""Unit tests for hunter.spotify, hunter.spgql, and hunter.spsync."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from hunter import library as lib_mod
from hunter import spgql, spsync
from hunter.config import HunterConfig, clear_config_cache
from hunter.db import reset_schema_cache
from hunter.http_client import HttpError
from hunter.spotify import (
    SpotifyClient,
    SpotifyError,
    SpotifyRateLimitError,
    _album_from_json,
)
from hunter.state import HunterState


def _make_cfg(tmp: Path, *, want_sources=("saved_albums", "playlists")) -> HunterConfig:
    beets_db = tmp / "musiclibrary.db"
    return HunterConfig(
        spotify_client_id="cid",
        spotify_client_secret="csec",
        token_file=tmp / "spotify_token.json",
        want_sources=tuple(want_sources),
        playlist_exclude="",
        slskd_url="http://slskd:5030",
        slskd_api_key="k",
        search_timeout=15,
        max_grabs_per_pass=4,
        min_candidate_ratio=0.8,
        max_candidate_ratio=2.5,
        prefer_lossless=True,
        min_bitrate=192,
        owned_coverage=0.8,
        max_attempts=3,
        search_miss_cap=8,
        peer_ban_strikes=3,
        drop_window_hours=48,
        backoff_seconds=(60, 120),
        grab_stale_seconds=3600,
        prowlarr_url="",
        prowlarr_api_key="",
        prowlarr_fallback=False,
        prowlarr_after_misses=2,
        sync_playlists=True,
        playlist_match_cap=100,
        playlist_prefix="",
        navidrome_url="http://nd:4533",
        navidrome_user="",
        navidrome_pass="",
        webhook_url="",
        notify_min_interval=1800,
        artist_check_hours=24,
        ui_port=0,
        ui_token="",
        apply=False,
        interval=1800,
        beets_db=beets_db,
        navidrome_db=tmp / "navidrome.db",
        audiomuse_url="",
        audiomuse_token="",
        quarantine_dir=tmp / "q",
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
        spotify_proxy="http://proxy:8888",
    )


def _resp(status: int = 200, json_data=None, text: str = "", headers: dict | None = None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status
    r.text = text
    r.headers = headers or {}
    if json_data is ValueError:
        r.json.side_effect = ValueError("bad json")
    else:
        r.json.return_value = json_data
    return r


class TestSpotifyClient(unittest.TestCase):
    def test_proxy_param_totp_retry_and_public_api(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tok_file = tmp / "spotify_token.json"
            tok_file.write_text(json.dumps({"sp_dc": "cookie123"}), encoding="utf-8")

            with patch.dict(os.environ, {"SPOTIFY_PROXY": "http://env-proxy:1234"}):
                with SpotifyClient("id", "sec", tok_file, proxy="") as sp:
                    self.assertEqual(sp.proxies, {})
                with SpotifyClient("id", "sec", tok_file, proxy="http://cfg-proxy:8888") as sp:
                    self.assertEqual(sp.proxies["https"], "http://cfg-proxy:8888")

                    reasons: list[str] = []
                    totps: list[str] = []

                    def fake_get(url, **kw):
                        reasons.append(kw["params"]["reason"])
                        totps.append(kw["params"]["totp"])
                        if kw["params"]["reason"] == "transport":
                            return _resp(403, text="URL Blocked")
                        return _resp(
                            200,
                            {
                                "accessToken": "web_tok_1",
                                "isAnonymous": False,
                                "accessTokenExpirationTimestampMs": int((time.time() + 3600) * 1000),
                            },
                        )

                    with patch("hunter.http_client.HttpSession.request", return_value=_resp(200, headers={"Date": "Thu, 01 Jan 1970 00:00:00 GMT"})), \
                         patch("hunter.http_client.HttpSession.get", side_effect=fake_get):
                        tok = sp.get_access_token()
                    self.assertEqual(tok, "web_tok_1")
                    self.assertEqual(reasons, ["transport", "init"])
                    # TOTP advanced to next 30s window on retry
                    self.assertNotEqual(totps[0], totps[1])

    def test_401_after_429_negative_retry_after_and_no_double_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tok_file = tmp / "spotify_token.json"
            tok_file.write_text(json.dumps({"refresh_token": "rt1"}), encoding="utf-8")

            with SpotifyClient("cid", "csec", tok_file, proxy="") as sp:
                sp._access_token = "tok0"
                sp._expires_at = time.monotonic() + 3600

                calls = {"n": 0}

                def fake_get(url, **kw):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return _resp(429, headers={"Retry-After": "-5"})
                    if calls["n"] == 2:
                        return _resp(401, text="expired")
                    return _resp(200, {"id": "me1"})

                with patch.object(sp._http, "get", side_effect=fake_get), \
                     patch.object(sp, "_refresh", side_effect=lambda: setattr(sp, "_access_token", "tok1") or setattr(sp, "_expires_at", time.monotonic() + 3600)), \
                     patch("hunter.spotify.time.sleep") as mock_sleep:
                    out = sp.me()
                self.assertEqual(out["id"], "me1")
                # Negative Retry-After clamped cleanly; one pace sleep before 401 retry and one before 200 retry
                self.assertEqual(mock_sleep.call_count, 2)
                self.assertTrue(all(c[0][0] >= 0.0 for c in mock_sleep.call_args_list))

    def test_rate_limit_exhaustion_raises_typed_error_without_final_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tok_file = tmp / "spotify_token.json"
            tok_file.write_text(json.dumps({"refresh_token": "rt1"}), encoding="utf-8")

            with SpotifyClient("cid", "csec", tok_file, proxy="") as sp:
                sp._RL_ATTEMPTS = 3
                sp._access_token = "tok0"
                sp._expires_at = time.monotonic() + 3600
                with patch.object(
                    sp._http,
                    "get",
                    return_value=_resp(429, headers={"Retry-After": "4"}),
                ), patch("hunter.spotify.time.sleep") as mock_sleep:
                    with self.assertRaises(SpotifyRateLimitError) as ctx:
                        sp.me()
                self.assertEqual(ctx.exception.retry_after, 4.0)
                # 3 attempts -> sleeps only before attempts 2 and 3 (never after final attempt)
                self.assertEqual(mock_sleep.call_count, 2)

                # Final attempt ending in 401 after 429 must raise SpotifyError, NOT SpotifyRateLimitError
                with patch.object(
                    sp._http,
                    "get",
                    side_effect=[_resp(429), _resp(429), _resp(401)],
                ), patch.object(sp, "_refresh"), patch("hunter.spotify.time.sleep"):
                    with self.assertRaises(SpotifyError) as err_ctx:
                        sp.me()
                    self.assertNotIsInstance(err_ctx.exception, SpotifyRateLimitError)

    def test_refresh_token_rotation_merges_without_wiping_sp_dc(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            tok_file = tmp / "spotify_token.json"
            tok_file.write_text(
                json.dumps({"sp_dc": "keep_me", "refresh_token": "rt_old"}),
                encoding="utf-8",
            )
            clear_config_cache()
            with SpotifyClient("cid", "csec", tok_file, proxy="") as sp:
                with patch.object(
                    sp._http,
                    "post",
                    return_value=_resp(
                        200,
                        {
                            "access_token": "acc_new",
                            "expires_in": 3600,
                            "refresh_token": "rt_new",
                        },
                    ),
                ):
                    # Force OAuth path by clearing sp_dc in memory return, while file still has sp_dc
                    with patch.object(sp, "_state", return_value={"refresh_token": "rt_old"}):
                        sp._refresh()
                saved = json.loads(tok_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["sp_dc"], "keep_me")
                self.assertEqual(saved["refresh_token"], "rt_new")
                self.assertEqual(tok_file.stat().st_mode & 0o777, 0o600)

    def test_null_safe_parsers(self) -> None:
        alb = _album_from_json({"id": "a1", "artists": [None, {"name": "Artist"}], "name": "Alb", "release_date": "2021"})
        self.assertEqual(alb.artist, "Artist")
        self.assertEqual(_album_from_json(None).id, "")

        with tempfile.TemporaryDirectory() as d:
            tok_file = Path(d) / "tok.json"
            with SpotifyClient("cid", "csec", tok_file, proxy="") as sp:
                with patch.object(
                    sp,
                    "_paginate",
                    return_value=[
                        {"track": None},
                        {"track": {"id": "t1", "name": "Song", "artists": [None], "album": None, "duration_ms": 180000}},
                    ],
                ):
                    tracks = sp.playlist_tracks("pl1")
                self.assertEqual(len(tracks), 1)
                self.assertEqual(tracks[0].artist, "")


class TestSpotifyGQL(unittest.TestCase):
    def test_http_200_persisted_query_not_found_self_heals(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ops_file = tmp / "sp_ops.json"
            ops_file.write_text(json.dumps({
                spgql.OP_LIBRARY: "oldhash",
                spgql.OP_LIKED: "oldhash2",
                spgql.OP_PLAYLIST: "oldhash3",
            }), encoding="utf-8")
            sp = MagicMock(spec=SpotifyClient)
            sp.proxies = {}
            sp.get_access_token.return_value = "tok"

            with spgql.SpotifyGQL(sp, tmp) as gql:
                calls = {"n": 0}

                def fake_post(url, **kw):
                    calls["n"] += 1
                    if calls["n"] == 1:
                        return _resp(200, {"errors": [{"message": "PersistedQueryNotFound"}]})
                    return _resp(200, {"data": {"me": {"libraryV3": {"items": [], "totalCount": 0}}}})

                new_ops = {
                    spgql.OP_LIBRARY: "newhash",
                    spgql.OP_LIKED: "newhash2",
                    spgql.OP_PLAYLIST: "newhash3",
                }
                with patch.object(gql._http, "post", side_effect=fake_post), \
                     patch.object(gql, "_extract_ops", return_value=new_ops) as mock_ext:
                    items, total = gql.library_page("Albums", 0)
                self.assertEqual(items, [])
                self.assertEqual(total, 0)
                mock_ext.assert_called_once()

    def test_bundle_allowlist_size_cap_and_parsers(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            sp = MagicMock(spec=SpotifyClient)
            sp.proxies = {}
            with spgql.SpotifyGQL(sp, tmp) as gql:
                html = (
                    '<script src="https://evil.example.com/bad.js"></script>'
                    '<script src="https://open.spotifycdn.com/huge.js"></script>'
                    '<script src="https://open.spotifycdn.com/app.js"></script>'
                )
                good_js = (
                    'new a.b("libraryV3","query","' + "a" * 64 + '");'
                    'new a.b("fetchLibraryTracks","query","' + "b" * 64 + '");'
                    'new a.b("fetchPlaylist","query","' + "c" * 64 + '");'
                )

                def fake_get(url, **kw):
                    if url == spgql.OPEN_URL:
                        return _resp(200, text=html)
                    if "evil.example.com" in url:
                        raise AssertionError("untrusted host should not be fetched")
                    if "huge.js" in url:
                        return _resp(200, text="x", headers={"Content-Length": str(20 * 1024 * 1024)})
                    return _resp(200, text=good_js)

                with patch.object(gql._http, "get", side_effect=fake_get):
                    ops = gql._extract_ops()
                self.assertEqual(ops[spgql.OP_LIBRARY], "a" * 64)
                self.assertTrue((tmp / "sp_ops.json").exists())

        # Strict URI filtering + null safety + album_artist/album_total
        self.assertIsNone(spgql.parse_track({"_uri": "spotify:local:foo:bar:1", "data": {"name": "x"}}))
        self.assertIsNone(spgql.parse_track({"_uri": "spotify:episode:ep1", "data": {"name": "pod"}}))
        self.assertIsNone(spgql.parse_track(None))
        parsed = spgql.parse_track({
            "_uri": "spotify:track:t1",
            "data": {
                "name": "Track",
                "artists": {"items": [None, {"profile": {"name": "Track Artist"}}]},
                "albumOfTrack": {
                    "uri": "spotify:album:alb1",
                    "name": "Alb",
                    "artists": {"items": [{"profile": {"name": "Album Artist"}}]},
                    "date": {"isoString": "2023-05-01T00:00:00Z"},
                    "tracks": {"totalCount": 12},
                },
                "trackDuration": {"totalMilliseconds": 200_000},
            },
        })
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["artist"], "Track Artist")
        self.assertEqual(parsed["album_artist"], "Album Artist")
        self.assertEqual(parsed["album_total"], 12)
        self.assertIsNone(spgql.parse_library_playlist(None))
        self.assertIsNone(spgql.parse_library_album({"item": None}))
        self.assertIsNone(spgql.parse_library_artist({"item": {"_uri": "spotify:artist:1", "data": None}}))


class TestSpSync(unittest.TestCase):
    def tearDown(self) -> None:
        lib_mod.invalidate_library_cache()
        reset_schema_cache()

    def test_ensure_job_respects_requested_scope_and_playlist_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_cfg(Path(d))
            with HunterState(cfg.state_db) as st:
                j1 = spsync.ensure_job(st, "all")
                j1["status"] = "partial"
                spsync._save(st, j1)

                # Different scope starts a new targeted job
                j2 = spsync.ensure_job(st, "playlist", "pl_99")
                self.assertEqual(j2["scope"], "playlist")
                self.assertEqual(j2["playlist_id"], "pl_99")
                self.assertEqual(j2["plan"], ["one_playlist"])

                # Same scope & playlist_id resumes
                j2["status"] = "partial"
                spsync._save(st, j2)
                j3 = spsync.ensure_job(st, "playlist", "pl_99")
                self.assertEqual(j3["gen"], j2["gen"])

    def test_page_done_handles_overreported_total_and_degraded_first_page(self) -> None:
        self.assertTrue(spsync._page_done([], 50, 52, "pl"))
        self.assertTrue(spsync._page_done([], 0, 0, "pl"))
        with self.assertRaises(SpotifyError):
            spsync._page_done([], 0, 10, "pl")

    def test_playlist_tracks_uses_done_ids_not_positional_idx_and_rate_limit_typed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            cfg.token_file.write_text(json.dumps({"sp_dc": "cookie"}), encoding="utf-8")

            with HunterState(cfg.state_db) as st:
                st.upsert_playlist("p1", "Beta", 1)
                st.upsert_playlist("p2", "Alpha", 1)

            # First run: rate-limit on p2 after completing p1 (or p2 first, then p1 renamed)
            with patch("hunter.spgql.SpotifyGQL") as mock_gql_cls, \
                 patch.object(lib_mod, "get_cached_library", return_value=None):
                gql = mock_gql_cls.return_value.__enter__.return_value
                gql.pace = 0.8

                def page_side_effect(pid, offset, limit=100):
                    if pid == "p2":
                        return ([{
                            "itemV2": {
                                "_uri": "spotify:track:t2",
                                "data": {
                                    "name": "Song 2",
                                    "artists": {"items": [{"profile": {"name": "Art"}}]},
                                    "albumOfTrack": {"uri": "spotify:album:a2", "name": "Alb2"},
                                    "duration": {"totalMilliseconds": 180_000},
                                },
                            }
                        }], 1)
                    raise SpotifyRateLimitError("429 quota", retry_after=10.0)

                gql.library_page.return_value = (
                    [
                        {
                            "item": {
                                "_uri": "spotify:playlist:p1",
                                "data": {"name": "Beta", "ownerV2": {"data": {"name": "u"}}, "contents": {"totalCount": 1}},
                            }
                        },
                        {
                            "item": {
                                "_uri": "spotify:playlist:p2",
                                "data": {"name": "Alpha", "ownerV2": {"data": {"name": "u"}}, "contents": {"totalCount": 1}},
                            }
                        },
                    ],
                    2,
                )
                gql.playlist_page.side_effect = page_side_effect
                job = spsync.run_sync_job(cfg, scope="playlists", max_requests=10)
                # First playlist in roster ("Alpha" = p2) completed, second ("Beta" = p1) hit rate limit
                self.assertEqual(job["status"], "cooldown")
                self.assertIn("p2", job["cursor"]["done_ids"])

            # Verify ignore_cooldown=True works and non-429 SpotifyError with "rate" word does NOT trigger cooldown
            with patch("hunter.spgql.SpotifyGQL") as mock_gql_cls, \
                 patch.object(lib_mod, "get_cached_library", return_value=None):
                gql = mock_gql_cls.return_value.__enter__.return_value
                gql.pace = 0.8
                gql.playlist_page.side_effect = SpotifyError("invalid bitrate field")
                job2 = spsync.run_sync_job(
                    cfg, scope="playlists", max_requests=10, ignore_cooldown=True
                )
                self.assertEqual(job2["status"], "error")

    def test_saved_albums_source_and_oauth_only_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            # OAuth refresh token only, no sp_dc -> clear error
            cfg.token_file.write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
            job = spsync.run_sync_job(cfg, scope="albums")
            self.assertEqual(job["status"], "error")
            self.assertIn("sp_dc", job["detail"])

            # With sp_dc -> saved albums use source "saved_albums", None items skipped cleanly
            cfg.token_file.write_text(json.dumps({"sp_dc": "cookie"}), encoding="utf-8")
            with patch("hunter.spgql.SpotifyGQL") as mock_gql_cls, \
                 patch.object(lib_mod, "get_cached_library", return_value=None):
                gql = mock_gql_cls.return_value.__enter__.return_value
                gql.pace = 0.8
                gql.library_page.return_value = (
                    [
                        None,
                        {
                            "item": {
                                "_uri": "spotify:album:alb1",
                                "data": {
                                    "name": "Dummy",
                                    "artists": {"items": [{"profile": {"name": "Portishead"}}]},
                                    "date": {"isoString": "1994-08-22"},
                                },
                            }
                        },
                    ],
                    1,
                )
                job2 = spsync.run_sync_job(cfg, scope="albums", ignore_cooldown=True)
                self.assertEqual(job2["status"], "done")
            with HunterState(cfg.state_db) as st:
                w = st.want_by_key("alb1")
                self.assertIsNotNone(w)
                self.assertEqual(w.source, "saved_albums")
                ov = spsync.overview(st, cfg)
                self.assertFalse(ov["busy"])

    def test_owned_gate_reflects_live_beets_library_and_lock_busy(self) -> None:
        import sqlite3
        from hunter.lock import hold

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            conn = sqlite3.connect(cfg.beets_db)
            conn.executescript(
                "CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT, album TEXT, mb_releasegroupid TEXT);"
                "CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, disc INTEGER, disctotal INTEGER DEFAULT 1, tracktotal INTEGER, format TEXT);"
                "INSERT INTO albums VALUES (1, 'Portishead', 'Dummy', 'rg-dummy');"
                "INSERT INTO items VALUES (1, 1, 1, 1, 1, 'FLAC');"
            )
            conn.commit()
            conn.close()

            lib_mod.invalidate_library_cache()
            self.assertTrue(spsync._have(cfg, "Portishead", "Dummy", 1))
            self.assertFalse(spsync._have(cfg, "Portishead", "Third", 10))

            with HunterState(cfg.state_db) as st:
                with hold(cfg.state_dir / "sp_sync.lock"):
                    self.assertTrue(spsync.overview(st, cfg)["busy"])
                self.assertFalse(spsync.overview(st, cfg)["busy"])

    def test_playlist_want_uses_album_artist_over_track_artist(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            cfg = _make_cfg(tmp)
            cfg.token_file.write_text(json.dumps({"sp_dc": "cookie"}), encoding="utf-8")
            with HunterState(cfg.state_db) as st:
                st.upsert_playlist("pl_aa", "Collab Mix", 1)
                st.set_playlist_flag("pl_aa", "hunt", True)

            with patch("hunter.spgql.SpotifyGQL") as mock_gql_cls, \
                 patch.object(lib_mod, "get_cached_library", return_value=None):
                gql = mock_gql_cls.return_value.__enter__.return_value
                gql.pace = 0.8
                gql.playlist_page.return_value = (
                    [
                        {
                            "itemV2": {
                                "_uri": "spotify:track:tr1",
                                "data": {
                                    "name": "Guest Track",
                                    "artists": {"items": [{"profile": {"name": "Featured Guest"}}]},
                                    "albumOfTrack": {
                                        "uri": "spotify:album:alb_aa",
                                        "name": "Main Album",
                                        "artists": {"items": [{"profile": {"name": "Main Album Artist"}}]},
                                    },
                                    "duration": {"totalMilliseconds": 200_000},
                                },
                            }
                        }
                    ],
                    1,
                )
                job = spsync.run_sync_job(cfg, scope="playlist", playlist_id="pl_aa")
                self.assertEqual(job["status"], "done")

            with HunterState(cfg.state_db) as st:
                w = st.want_by_key("alb_aa")
                self.assertIsNotNone(w)
                self.assertEqual(w.artist, "Main Album Artist")


if __name__ == "__main__":
    unittest.main()

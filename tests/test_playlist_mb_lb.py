"""Unit tests for hunter.playlist_sync, hunter.mb, and hunter.listenbrainz."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from hunter import listenbrainz as lb
from hunter import mb
from hunter.config import clear_config_cache
from hunter.db import reset_schema_cache
from hunter.http_client import HttpError, default_user_agent
from hunter.playlist_sync import (
    SubsonicClient,
    SubsonicError,
    _artist_set,
    match_song,
    mirror_playlists,
    resolve_track,
)
from hunter.spotify import SpAlbum, SpPlaylist, SpTrack
from hunter.state import HunterState


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


class TestPlaylistSync(unittest.TestCase):
    def tearDown(self) -> None:
        reset_schema_cache()

    def test_subsonic_client_post_form_and_error_redaction(self) -> None:
        with SubsonicClient("http://nd:4533", "u", "supersecret") as sub:
            with patch.object(
                sub._http,
                "get_json",
                side_effect=HttpError("http://nd:4533/rest/ping?p=supersecret"),
            ):
                with self.assertRaises(SubsonicError) as ctx:
                    sub.playlists()
                self.assertNotIn("supersecret", str(ctx.exception))

            song_ids = [f"s{i}" for i in range(600)]
            with patch.object(
                sub._http,
                "post_json",
                return_value={"subsonic-response": {"status": "ok", "playlist": {"id": "pl-42"}}},
            ) as mock_post:
                pid = sub.create_or_replace("Big List", song_ids, None)
                self.assertEqual(pid, "pl-42")
                mock_post.assert_called_once()
                self.assertEqual(len(mock_post.call_args.kwargs["data"]["songId"]), 600)

    def test_artist_splitting_and_match_song_guards(self) -> None:
        self.assertEqual(_artist_set("Florence and the Machine"), {"florence and the machine"})
        self.assertEqual(_artist_set("Simon & Garfunkel"), {"simon garfunkel"})
        self.assertEqual(_artist_set("feat. Guest"), {"guest"})

        # Empty title key must not match anything
        blank_track = SpTrack("t0", "(feat. Someone)", "Artist", 200, SpAlbum("a", "Artist", "Alb", 2020, 10))
        self.assertIsNone(match_song(blank_track, [{"id": "s1", "title": "(feat. Other)", "artist": "Artist", "duration": 200}]))

        # "Pink" must NOT prefix-match "Pink Floyd"
        pink_track = SpTrack("t1", "So What (Explicit)", "Pink", 215, SpAlbum("a", "Pink", "Funhouse", 2008, 12))
        self.assertIsNone(
            match_song(
                pink_track,
                [{"id": "s2", "title": "So What", "artist": "Pink Floyd", "duration": 215}],
            )
        )

        # Long multi-word prefix still matches: "Lilly Palmer" vs "Lilly Palmer Dann"
        lp_track = SpTrack("t2", "Resonate (Original Mix)", "Lilly Palmer", 300, SpAlbum("a", "Lilly Palmer", "Alb", 2022, 1))
        self.assertEqual(
            match_song(
                lp_track,
                [{"id": "s3", "title": "Resonate", "artist": "Lilly Palmer Dann", "duration": 300}],
            ),
            "s3",
        )

    def test_resolve_track_title_fallback_when_title_artist_has_non_matching_results(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with HunterState(Path(d) / "hunter.db") as st:
                sub = MagicMock(spec=SubsonicClient)
                sub.search_songs.side_effect = [
                    [{"id": "wrong", "title": "Other Song", "artist": "Artist", "duration": 200}],
                    [{"id": "right", "title": "Target", "artist": "Artist", "duration": 200}],
                ]
                tr = SpTrack("tid1", "Target", "Artist", 200, SpAlbum("a", "Artist", "Alb", 2020, 1))
                sid, did = resolve_track(sub, st, tr)
                self.assertEqual(sid, "right")
                self.assertTrue(did)
                self.assertEqual(sub.search_songs.call_count, 2)

    def test_mirror_playlists_skips_truncated_and_isolates_errors(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with HunterState(Path(d) / "hunter.db") as st:
                spotify = MagicMock()
                spotify.playlists.return_value = [
                    SpPlaylist("p_trunc", "Truncated", 2),
                    SpPlaylist("p_fail", "Failing", 1),
                    SpPlaylist("p_ok", "Working", 1),
                ]
                t1 = SpTrack("t1", "One", "Art", 180, SpAlbum("a1", "Art", "Alb", 2020, 1))
                t2 = SpTrack("t2", "Two", "Art", 180, SpAlbum("a1", "Art", "Alb", 2020, 1))
                t3 = SpTrack("t3", "Three", "Art", 180, SpAlbum("a1", "Art", "Alb", 2020, 1))
                spotify.playlist_tracks.side_effect = [
                    [t1, t2],
                    RuntimeError("spotify read failed"),
                    [t3],
                ]
                st.set_track_match("t3", "nd-3")  # already cached

                sub = MagicMock(spec=SubsonicClient)
                sub.playlists_multi.return_value = {"Truncated": ["nd-pl-1"]}
                st.set_meta("nd_playlists", '{"Truncated": "nd-pl-1"}')
                sub.search_songs.return_value = [
                    {"id": "nd-1", "title": "One", "artist": "Art", "duration": 180}
                ]
                sub.create_or_replace.return_value = "nd-pl-ok"

                # lookup_cap=1 -> p_trunc resolves t1 (1 lookup), hits cap on uncached t2 -> must NOT overwrite!
                stats = mirror_playlists(
                    spotify,
                    sub,
                    st,
                    exclude_re="",
                    prefix="",
                    lookup_cap=1,
                    apply=True,
                    deadline=time.time() + 60,
                )
                self.assertEqual(stats.errors, 1)
                sub.create_or_replace.assert_called_once_with("Working", ["nd-3"], None)

    def test_mirror_playlists_skips_overwrite_on_transient_subsonic_error_and_passes_miss_retry(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with HunterState(Path(d) / "hunter.db") as st:
                spotify = MagicMock()
                spotify.playlists.return_value = [SpPlaylist("p1", "Mix", 2)]
                t1 = SpTrack("t1", "One", "Art", 180, SpAlbum("a1", "Art", "Alb", 2020, 1))
                t2 = SpTrack("t2", "Two", "Art", 180, SpAlbum("a1", "Art", "Alb", 2020, 1))
                spotify.playlist_tracks.return_value = [t1, t2]

                sub = MagicMock(spec=SubsonicClient)
                sub.playlists_multi.return_value = {"Mix": ["nd-mix"]}
                st.set_meta("nd_playlists", '{"Mix": "nd-mix"}')
                sub.search_songs.side_effect = [
                    [{"id": "nd-1", "title": "One", "artist": "Art", "duration": 180}],
                    SubsonicError("transient timeout"),
                ]

                with patch.object(st, "get_track_match", wraps=st.get_track_match) as mock_gtm:
                    mirror_playlists(
                        spotify,
                        sub,
                        st,
                        exclude_re="",
                        prefix="",
                        lookup_cap=50,
                        apply=True,
                        deadline=time.time() + 60,
                        miss_retry_s=12345,
                    )
                sub.create_or_replace.assert_not_called()
                self.assertTrue(
                    any(c.kwargs.get("miss_retry_s") == 12345 for c in mock_gtm.call_args_list)
                )


class TestMusicBrainz(unittest.TestCase):
    def setUp(self) -> None:
        with mb._cache_lock:
            mb._cache.clear()
        with mb._rate_lock:
            mb._next_slot = 0.0

    def test_user_agent_dynamic_contact_and_thread_local_session(self) -> None:
        clear_config_cache()
        with patch.dict(os.environ, {"HUNTER_USER_AGENT_CONTACT": "ops@example.org"}):
            clear_config_cache()
            ua = mb._user_agent()
            self.assertIn("ops@example.org", ua)
            self.assertNotIn("gmail.com", ua)
            sessions: list[object] = []

            def grab():
                sessions.append(mb._session())

            t1 = threading.Thread(target=grab)
            t2 = threading.Thread(target=grab)
            t1.start(); t2.start()
            t1.join(); t2.join()
            self.assertEqual(len(sessions), 2)
            self.assertIsNot(sessions[0], sessions[1])
        clear_config_cache()

    def test_rg_track_count_handles_mb_unavailable(self) -> None:
        with patch.object(mb, "mb_get", side_effect=mb.MBUnavailable("503")):
            self.assertIsNone(mb.rg_track_count("rg-1"))

    def test_search_escapes_lucene_and_guards_year(self) -> None:
        captured: list[str] = []

        def fake_mb_get(path, params):
            captured.append(params["query"])
            if path == "/recording":
                return {
                    "recordings": [
                        {
                            "title": "Thunderstruck",
                            "artist-credit": [{"name": "AC/DC"}],
                            "releases": [
                                {
                                    "date": "199?",
                                    "status": "Official",
                                    "release-group": {
                                        "id": "rg1",
                                        "title": "The Razors Edge",
                                        "primary-type": "Album",
                                        "first-release-date": "????",
                                    },
                                }
                            ],
                        }
                    ]
                }
            return {"artists": []}

        with patch.object(mb, "mb_get", side_effect=fake_mb_get):
            mb.search("artist", "AC/DC (Live): Re!Zero")
            self.assertIn(r"AC\/DC", captured[-1])
            self.assertIn(r"\(Live\)\:", captured[-1])
            self.assertIn(r"Re\!Zero", captured[-1])

            songs = mb.search("song", "Panic! at the Disco")
            self.assertEqual(len(songs), 1)
            self.assertEqual(songs[0]["year"], 0)

    def test_artist_release_groups_paginates(self) -> None:
        page1 = [{"id": f"rg-{i}", "title": f"A{i}", "first-release-date": "2020"} for i in range(100)]
        page2 = [{"id": "rg-100", "title": "A100", "first-release-date": "2024"}]
        with patch.object(
            mb,
            "mb_get",
            side_effect=[{"release-groups": page1}, {"release-groups": page2}],
        ) as mock_get:
            rgs = mb.artist_release_groups("art-1")
        self.assertEqual(len(rgs), 101)
        self.assertEqual(mock_get.call_count, 2)

    def test_mb_get_caps_retry_after_and_no_sleep_after_final_attempt(self) -> None:
        clock = {"now": 100.0}
        sleeps: list[float] = []

        def fake_sleep(secs: float) -> None:
            sleeps.append(secs)
            clock["now"] += secs

        with patch.object(
            mb._session(),
            "get",
            return_value=_resp(503, headers={"Retry-After": "999"}),
        ), patch("hunter.mb.time.sleep", side_effect=fake_sleep), \
             patch("hunter.mb.time.monotonic", side_effect=lambda: clock["now"]):
            with self.assertRaises(mb.MBUnavailable):
                mb.mb_get("/artist", {"query": "test_no_sleep"})
        # 4 attempts -> retry sleeps only on attempts 0, 1, 2 (each capped at 30s)
        retry_sleeps = [s for s in sleeps if s >= 10.0]
        self.assertEqual(len(retry_sleeps), 3)
        self.assertTrue(all(s <= 30.0 for s in retry_sleeps))

    def test_mb_get_releases_rate_lock_before_http_io(self) -> None:
        observed_locked: list[bool] = []

        def fake_get(url, **kw):
            observed_locked.append(mb._rate_lock.locked())
            return _resp(200, {"artists": []})

        with patch.object(mb._session(), "get", side_effect=fake_get):
            mb.mb_get("/artist", {"query": "lock_check"})
        self.assertEqual(observed_locked, [False])


class TestListenBrainz(unittest.TestCase):
    def setUp(self) -> None:
        with lb._rate_lock:
            lb._next_slot = 0.0

    def test_invalid_json_shapes_and_partial_batch_do_not_poison_canon(self) -> None:
        rgs = [{"rgid": f"rg-{i}", "album": f"Alb {i}", "year": 2020} for i in range(60)]
        batch1_resp = _resp(
            200,
            [
                None,
                "bad",
                {"release_group_mbid": "rg-0", "total_listen_count": 1000},
                {"release_group_mbid": "", "total_listen_count": 50},
            ],
        )
        batch2_resp = _resp(500, text="server error")

        with patch.object(lb._session(), "post", side_effect=[batch1_resp, batch2_resp]), \
             patch("hunter.listenbrainz.time.sleep"):
            scored, complete = lb.canon_scores_checked("mbid-1", rgs)
        self.assertFalse(complete)
        self.assertEqual(len(scored), 1)

        with patch.object(lb._session(), "post", side_effect=[batch1_resp, batch2_resp]), \
             patch("hunter.listenbrainz.time.sleep"):
            self.assertEqual(lb.canon_scores("mbid-1", rgs), [])

    def test_empty_mbids_filtered_and_complete_batch_succeeds(self) -> None:
        rgs = [
            {"rgid": "", "album": "Blank", "year": 2020},
            {"rgid": "rg-1", "album": "Top", "year": 2021},
            {"rgid": "rg-2", "album": "Mid", "year": 2022},
        ]
        ok_resp = _resp(
            200,
            [
                {"release_group_mbid": "rg-1", "total_listen_count": 1000},
                {"release_group_mbid": "rg-2", "total_listen_count": 100},
            ],
        )
        with patch.object(lb._session(), "post", return_value=ok_resp) as mock_post, \
             patch("hunter.listenbrainz.time.sleep"):
            out = lb.canon_scores("mbid-1", rgs)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["score"], 1.0)
        sent = mock_post.call_args.kwargs["json"]["release_group_mbids"]
        self.assertEqual(sent, ["rg-1", "rg-2"])


if __name__ == "__main__":
    unittest.main()

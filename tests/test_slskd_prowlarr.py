"""Unit tests for hunter.slskd_client and hunter.prowlarr."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import requests

from hunter import __version__
from hunter.http_client import HttpError
from hunter.prowlarr import ProwlarrClient, ProwlarrError, Release, _parse, rank
from hunter.slskd_client import (
    Candidate,
    SlskdClient,
    SlskdError,
    _avg_bitrate,
    rank_candidates,
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


class TestSlskdClient(unittest.TestCase):
    def test_context_manager_ua_and_error_wrapping(self) -> None:
        with SlskdClient("http://slskd:5030", "secret-key") as client:
            self.assertIn(__version__, client._http.headers["User-Agent"])
            with patch.object(
                client._http,
                "request_json",
                side_effect=HttpError("boom", status=502),
            ):
                with self.assertRaises(SlskdError) as ctx:
                    client.downloads()
                self.assertEqual(ctx.exception.status, 502)

    def test_enqueue_url_quotes_username_and_validates(self) -> None:
        client = SlskdClient("http://slskd:5030", "k")
        try:
            with self.assertRaises(SlskdError):
                client.enqueue("user", [])
            with patch.object(client._http, "request") as mock_req:
                client.enqueue(
                    "user/with spaces?#",
                    [{"filename": "dir/01.flac", "size": 1234}],
                )
                mock_req.assert_called_once()
                url = mock_req.call_args[0][1]
                self.assertTrue(
                    url.endswith("/api/v0/transfers/downloads/user%2Fwith%20spaces%3F%23")
                )
        finally:
            client.close()

    def test_cancel_dir_normalizes_slashes_quotes_and_skips_empty_id(self) -> None:
        client = SlskdClient("http://slskd:5030", "k")
        downloads_payload = [
            {
                "username": "peer/one",
                "directories": [
                    {
                        "files": [
                            {"id": None, "filename": r"Music\Artist\Album\01.flac", "state": "Queued"},
                            {"id": "", "filename": r"Music\Artist\Album\02.flac", "state": "Queued"},
                            {"id": "ok-1", "filename": r"Music\Artist\Album\03.flac", "state": "Completed, Succeeded"},
                            {"id": "id/with?special", "filename": r"Music\Artist\Album\04.flac", "state": "InProgress"},
                        ]
                    }
                ],
            }
        ]
        try:
            with patch.object(client, "downloads", return_value=downloads_payload), \
                 patch.object(client._http, "request") as mock_req:
                removed = client.cancel_dir("peer/one", "Music/Artist/Album")
                self.assertEqual(removed, 1)
                mock_req.assert_called_once()
                url = mock_req.call_args[0][1]
                self.assertIn("/transfers/downloads/peer%2Fone/id%2Fwith%3Fspecial?remove=true", url)
        finally:
            client.close()

    def test_search_sends_timeout_survives_poll_hiccup_and_cleans_up(self) -> None:
        client = SlskdClient("http://slskd:5030", "k")
        calls: list[tuple[str, str]] = []

        def fake_req_json(method, path, **kw):
            calls.append((method, path))
            if method == "POST" and path == "/searches":
                self.assertEqual(kw["json"]["searchTimeout"], 15000)
                return {"id": "srch/1"}
            if path == "/searches/srch%2F1" and len([c for c in calls if c[1] == "/searches/srch%2F1"]) == 1:
                raise SlskdError("502 bad gateway", status=502)
            if path == "/searches/srch%2F1":
                return {"isComplete": True}
            if path == "/searches/srch%2F1/responses":
                return [{"username": "u1", "files": []}]
            return {}

        try:
            with patch.object(client, "_req_json", side_effect=fake_req_json), \
                 patch.object(client, "_req") as mock_del, \
                 patch("hunter.slskd_client.time.sleep") as mock_sleep:
                res = client.search("artist album", 15)
                self.assertEqual(len(res), 1)
                self.assertGreaterEqual(mock_sleep.call_count, 2)
                mock_del.assert_called_once_with("DELETE", "/searches/srch%2F1")
        finally:
            client.close()

    def test_download_states_normalizes_and_reports_terminal_states(self) -> None:
        client = SlskdClient("http://slskd:5030", "k")
        payload = [
            {
                "username": "p1",
                "directories": [
                    {
                        "files": [
                            {"filename": r"a\b\01.flac", "state": "Completed, Succeeded"},
                            {"filename": r"a\b\02.flac", "state": "Completed, Errored"},
                        ]
                    }
                ],
            },
            {
                "username": "p2",
                "directories": [
                    {
                        "files": [
                            {"filename": r"c\d\01.mp3", "state": "Completed, Cancelled"},
                            {"filename": r"c\d\02.mp3", "state": "Completed, TimedOut"},
                        ]
                    }
                ],
            },
            {
                "username": "p3",
                "directories": [
                    {
                        "files": [
                            {"filename": "e/f/01.mp3", "state": "InProgress", "percentComplete": "bad"},
                        ]
                    }
                ],
            },
        ]
        try:
            with patch.object(client, "downloads", return_value=payload):
                st = client.download_states()
            self.assertEqual(st[("p1", "a/b")]["state"], "partial_error")
            self.assertEqual(st[("p1", "a/b")]["succeeded"], 1)
            self.assertEqual(st[("p1", "a/b")]["errored"], 1)
            self.assertEqual(st[("p2", "c/d")]["state"], "failed")
            self.assertEqual(st[("p3", "e/f")]["state"], "active")
            self.assertEqual(st[("p3", "e/f")]["pct"], 0.0)
        finally:
            client.close()

    def test_single_track_matching(self) -> None:
        responses = [
            {
                "username": "peer1",
                "hasFreeUploadSlot": True,
                "queueLength": 0,
                "uploadSpeed": 1000,
                "files": [{"filename": "Artist/Single/01.flac", "size": 25_000_000}],
            }
        ]
        cands = rank_candidates(
            responses,
            expected_tracks=1,
            min_ratio=0.8,
            max_ratio=2.5,
            prefer_lossless=True,
            exclude=set(),
        )
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].audio_count, 1)

    def test_avg_bitrate_and_zero_byte_rejection(self) -> None:
        # Mixed files: only 1 of 3 has length -> reported must be False,
        # and size/length only uses the file with length > 0 (not inflated by the others).
        files_mixed = [
            {"filename": "d/1.mp3", "size": 4_000_000, "length": 100},  # 320 kbps
            {"filename": "d/2.mp3", "size": 8_000_000, "length": 0},
            {"filename": "d/3.mp3", "size": 8_000_000},
        ]
        kbps, reported = _avg_bitrate(files_mixed)
        self.assertEqual(kbps, 320)
        self.assertFalse(reported)

        # >= half have length -> reported True
        files_half = [
            {"filename": "d/1.mp3", "size": 4_000_000, "length": 100},
            {"filename": "d/2.mp3", "size": 4_000_000, "length": 100},
            {"filename": "d/3.mp3", "size": 4_000_000},
        ]
        kbps2, reported2 = _avg_bitrate(files_half)
        self.assertEqual(kbps2, 320)
        self.assertTrue(reported2)

        # 0-byte corrupt lossy files rejected when min_bitrate > 0
        responses = [
            {
                "username": "corrupt",
                "hasFreeUploadSlot": True,
                "files": [
                    {"filename": "a/01.mp3", "size": 0},
                    {"filename": "a/02.mp3", "size": 0},
                ],
            }
        ]
        cands = rank_candidates(
            responses,
            expected_tracks=2,
            min_ratio=0.8,
            max_ratio=2.5,
            prefer_lossless=False,
            exclude=set(),
            min_bitrate=192,
        )
        self.assertEqual(cands, [])

        # 0-byte corrupt FLAC directory rejected unconditionally (even min_bitrate=0)
        corrupt_flac = [
            {
                "username": "corrupt_flac",
                "hasFreeUploadSlot": True,
                "files": [
                    {"filename": "a/01.flac", "size": 0},
                    {"filename": "a/02.flac", "size": 0},
                ],
            }
        ]
        self.assertEqual(
            rank_candidates(
                corrupt_flac,
                expected_tracks=2,
                min_ratio=0.8,
                max_ratio=2.5,
                prefer_lossless=True,
                exclude=set(),
                min_bitrate=0,
            ),
            [],
        )

    def test_prefer_lossless_false_does_not_give_lossless_minus_32(self) -> None:
        responses = [
            {
                "username": "flac_queued",
                "hasFreeUploadSlot": False,
                "queueLength": 20,
                "uploadSpeed": 100,
                "files": [
                    {"filename": "alb/01.flac", "size": 30_000_000, "bitRate": 950},
                    {"filename": "alb/02.flac", "size": 30_000_000, "bitRate": 950},
                ],
            },
            {
                "username": "mp3_free",
                "hasFreeUploadSlot": True,
                "queueLength": 0,
                "uploadSpeed": 500,
                "files": [
                    {"filename": "alb/01.mp3", "size": 8_000_000, "bitRate": 320},
                    {"filename": "alb/02.mp3", "size": 8_000_000, "bitRate": 320},
                ],
            },
        ]
        ranked = rank_candidates(
            responses,
            expected_tracks=2,
            min_ratio=0.8,
            max_ratio=2.5,
            prefer_lossless=False,
            exclude=set(),
        )
        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked[0].username, "mp3_free")


class TestProwlarr(unittest.TestCase):
    def test_client_context_and_error_wrapping(self) -> None:
        with ProwlarrClient("http://prowlarr:9696", "api-key") as client:
            with patch.object(
                client._http, "get_json", side_effect=HttpError("timeout")
            ):
                with self.assertRaises(ProwlarrError):
                    client.search("test")

    def test_parse_protocol_lowercase_and_lossy_transcode_rejection(self) -> None:
        r_transcode = _parse({
            "title": "Artist - Album (2020) [MP3 320kbps from FLAC]",
            "guid": "g1",
            "indexerId": 1,
            "indexer": "RED",
            "protocol": "Torrent",
            "size": 100_000_000,
            "seeders": 5,
            "categories": [{"id": 3040}],
        })
        self.assertEqual(r_transcode.protocol, "torrent")
        self.assertFalse(r_transcode.lossless)

        r_real_flac = _parse({
            "title": "Artist - Album (2020) [FLAC 24bit]",
            "guid": "g2",
            "indexerId": 2,
            "indexer": "NZB",
            "protocol": "USENET",
            "size": 0,
            "categories": [{"id": 3000}],
        })
        self.assertEqual(r_real_flac.protocol, "usenet")
        self.assertTrue(r_real_flac.lossless)

    def test_rank_short_names_substring_rejection_and_sizes(self) -> None:
        def rel(title: str, size: int = 120_000_000) -> Release:
            return Release(
                title=title,
                guid=title,
                indexer_id=1,
                indexer="idx",
                protocol="torrent",
                size=size,
                seeders=10,
                lossless=True,
                age_hours=1.0,
            )

        # Short names: U2, R.E.M. (REM), The XX, OK Go
        self.assertEqual(
            len(rank([rel("U2 - Joshua Tree (1987) FLAC")], "U2", "Joshua Tree",
                     prefer_lossless=True, lossless_only=False, tried=set())),
            1,
        )
        self.assertEqual(
            len(rank([rel("REM - Automatic For The People FLAC")], "R.E.M.", "Automatic For The People",
                     prefer_lossless=True, lossless_only=False, tried=set())),
            1,
        )
        self.assertEqual(
            len(rank([rel("The XX - XX FLAC")], "The XX", "XX",
                     prefer_lossless=True, lossless_only=False, tried=set())),
            1,
        )

        # Substring false positive: "Air" must NOT match "Chair"
        self.assertEqual(
            rank([rel("Chair - Moon Safari FLAC")], "Air", "Moon Safari",
                 prefer_lossless=True, lossless_only=False, tried=set()),
            [],
        )

        # Empty artist tokens fail closed
        self.assertEqual(
            rank([rel("Something FLAC")], "!!!", "Something",
                 prefer_lossless=True, lossless_only=False, tried=set()),
            [],
        )

        # Size == 0 allowed; EP >= 3MB allowed; < 3MB junk dropped
        ep_ok = rel("Air - Moon Safari EP FLAC", size=5_000_000)
        unknown_sz = rel("Air - Moon Safari FLAC", size=0)
        spam_tiny = rel("Air - Moon Safari FLAC", size=500_000)
        kept = rank(
            [ep_ok, unknown_sz, spam_tiny],
            "Air",
            "Moon Safari",
            prefer_lossless=True,
            lossless_only=False,
            tried=set(),
        )
        self.assertEqual(len(kept), 2)
        self.assertNotIn(spam_tiny, kept)


if __name__ == "__main__":
    unittest.main()

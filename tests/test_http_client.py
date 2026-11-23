"""Tests for hunter.http_client and hunter.notify."""
from __future__ import annotations

import email.utils
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

import hunter
from hunter.http_client import (
    HttpError,
    HttpSession,
    RateLimitError,
    default_user_agent,
    parse_retry_after,
)
from hunter.notify import Notifier


class RetryAfterAndUaTests(unittest.TestCase):
    def test_parse_retry_after_handles_numbers_dates_negative_and_cap(self):
        self.assertEqual(parse_retry_after(None, default=7.0), 7.0)
        self.assertEqual(parse_retry_after("12"), 12.0)
        self.assertEqual(parse_retry_after("2.5"), 2.5)
        self.assertEqual(parse_retry_after("-5"), 0.0)
        self.assertEqual(parse_retry_after("999", cap=60.0), 60.0)
        future = email.utils.formatdate(time.time() + 10, usegmt=True)
        parsed = parse_retry_after(future, cap=60.0)
        self.assertGreater(parsed, 5.0)
        self.assertLessEqual(parsed, 12.0)
        past = email.utils.formatdate(time.time() - 30, usegmt=True)
        self.assertEqual(parse_retry_after(past), 0.0)

    def test_default_user_agent_uses_version_and_optional_contact(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            ua = default_user_agent(None, "mb")
            self.assertEqual(ua, f"Hunter-mb/{hunter.__version__}")
            cfg = SimpleNamespace(user_agent_contact="admin@example.org")
            self.assertEqual(
                default_user_agent(cfg, "hunter"),
                f"Hunter-hunter/{hunter.__version__} (admin@example.org)",
            )
        with mock.patch.dict(os.environ, {"HUNTER_APP_NAME": "CustomApp"}):
            self.assertEqual(
                default_user_agent(None, "mb"),
                f"CustomApp-mb/{hunter.__version__}",
            )


class HttpSessionTests(unittest.TestCase):
    def test_context_manager_json_and_errors(self):
        resp_ok = mock.Mock(status_code=200, headers={})
        resp_ok.json.return_value = {"ok": True}
        resp_bad_json = mock.Mock(status_code=200, headers={})
        resp_bad_json.json.side_effect = ValueError("bad json")
        resp_429 = mock.Mock(status_code=429, headers={"Retry-After": "3"})

        with HttpSession(timeout=5.0, proxy="http://proxy:8888") as s:
            self.assertEqual(s.proxies["https"], "http://proxy:8888")
            with mock.patch.object(s._session, "request", return_value=resp_ok):
                self.assertEqual(s.get_json("http://example.test/api"), {"ok": True})
            with mock.patch.object(s._session, "request", return_value=resp_bad_json):
                with self.assertRaises(HttpError) as ctx:
                    s.get_json("http://example.test/api")
                self.assertEqual(ctx.exception.status, 200)
            with mock.patch.object(s._session, "request", return_value=resp_429):
                with self.assertRaises(RateLimitError) as ctx:
                    s.get("http://example.test/api")
                self.assertEqual(ctx.exception.status, 429)
                self.assertEqual(ctx.exception.retry_after, 3.0)

    def test_429_retry_paces_without_double_sleep(self):
        resp_429 = mock.Mock(status_code=429, headers={"Retry-After": "2"})
        resp_200 = mock.Mock(status_code=200, headers={})
        resp_200.json.return_value = {"done": 1}

        sleeps: list[float] = []
        with HttpSession(min_interval=1.0, max_retries=1) as s:
            with (
                mock.patch.object(s._session, "request", side_effect=[resp_429, resp_200]),
                mock.patch("hunter.http_client.time.sleep", side_effect=sleeps.append),
                mock.patch("hunter.http_client.time.monotonic", side_effect=[10.0, 12.0, 12.0]),
            ):
                self.assertEqual(s.get_json("http://example.test/api"), {"done": 1})
        self.assertEqual(sleeps, [2.0])


class NotifierTests(unittest.TestCase):
    def test_ntfy_discord_gotify_buckets_and_force(self):
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d)
            with mock.patch("requests.Session.request") as req:
                req.return_value = mock.Mock(status_code=200, headers={})

                n_ntfy = Notifier("http://ntfy.local/topic", 3600, sd)
                self.assertTrue(n_ntfy.send("routine 1", bucket="routine"))
                self.assertFalse(n_ntfy.send("routine 2", bucket="routine"))
                # Alert bucket is independent of routine bucket!
                self.assertTrue(n_ntfy.send("critical!", bucket="alert"))
                # force=True bypasses rate limit
                self.assertTrue(n_ntfy.send("forced!", force=True, bucket="routine"))
                self.assertEqual(req.call_count, 3)

                n_discord = Notifier("https://discord.com/api/webhooks/123/abc", 0, sd)
                self.assertTrue(n_discord.send("hello discord", force=True))
                self.assertEqual(req.call_args.kwargs["json"], {"content": "hello discord"})

                n_gotify = Notifier("https://gotify.local/message?token=xyz", 0, sd)
                self.assertTrue(n_gotify.send("hello gotify", force=True, title="t", priority=8))
                self.assertEqual(
                    req.call_args.kwargs["json"],
                    {"title": "t", "message": "hello gotify", "priority": 8},
                )

    def test_failed_post_does_not_burn_rate_limit(self):
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d)
            n = Notifier("http://ntfy.local/topic", 3600, sd)
            with mock.patch("requests.Session.request", side_effect=requests.ConnectionError("down")):
                self.assertFalse(n.send("err"))
            with mock.patch("requests.Session.request") as req:
                req.return_value = mock.Mock(status_code=200, headers={})
                self.assertTrue(n.send("recovered"))


if __name__ == "__main__":
    unittest.main()

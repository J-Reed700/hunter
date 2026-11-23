"""Tests for hunter.auth (atomic setup, timing resistance, bounded lockout, cookies)."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hunter import auth as auth_mod
from hunter.auth import (
    Auth,
    AuthError,
    check_token_lockout,
    clear_cookie,
    clear_token_failure,
    record_token_failure,
    session_cookie,
    summarize_body,
)
from hunter.db import reset_schema_cache


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hunter.db"
        reset_schema_cache()
        with auth_mod._fails_lock:
            auth_mod._fails.clear()

    def tearDown(self):
        reset_schema_cache()
        with auth_mod._fails_lock:
            auth_mod._fails.clear()
        self.tmp.cleanup()

    def test_create_first_admin_is_atomic_under_concurrency(self):
        # Pre-warm DB schema
        Auth(self.db_path).close()

        outcomes: list[str] = []
        lock = threading.Lock()

        def try_setup(idx: int):
            with Auth(self.db_path) as a:
                try:
                    a.create_first_admin(f"admin{idx}", "supersecret99")
                    res = "ok"
                except AuthError as exc:
                    res = f"err:{exc.code}"
            with lock:
                outcomes.append(res)

        threads = [threading.Thread(target=try_setup, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("err:409"), 3)
        with Auth(self.db_path) as a:
            self.assertEqual(a.user_count(), 1)

    def test_authenticate_uniform_error_for_missing_wrong_pw_and_disabled(self):
        with Auth(self.db_path) as a:
            u = a.create_first_admin("alice", "correct-horse-99")
            a.create_user("bob", "correct-horse-99", role="viewer")
            a.update_user(u["id"] + 1, disabled=True)

            with mock.patch("hunter.auth.time.sleep"):
                with self.assertRaises(AuthError) as e_missing:
                    a.authenticate("nobody", "pw")
                with self.assertRaises(AuthError) as e_badpw:
                    a.authenticate("alice", "wrongpw")
                with self.assertRaises(AuthError) as e_disabled:
                    a.authenticate("bob", "correct-horse-99")

            self.assertEqual(str(e_missing.exception), "invalid credentials")
            self.assertEqual(str(e_badpw.exception), "invalid credentials")
            self.assertEqual(str(e_disabled.exception), "invalid credentials")
            self.assertEqual(
                (e_missing.exception.code, e_badpw.exception.code, e_disabled.exception.code),
                (401, 401, 401),
            )

    def test_bounded_lockout_eviction_and_token_throttle(self):
        # Fill above capacity: must evict oldest entries without wiping recent lockouts
        with mock.patch.object(auth_mod, "_MAX_THROTTLE_ENTRIES", 5):
            for i in range(6):
                record_token_failure(f"10.0.0.{i}")
            for _ in range(auth_mod.LOCKOUT_AFTER):
                record_token_failure("10.0.0.99")
            self.assertGreater(check_token_lockout("10.0.0.99"), 0)
            self.assertEqual(len(auth_mod._fails), 5)
            clear_token_failure("10.0.0.99")
            self.assertEqual(check_token_lockout("10.0.0.99"), 0)

    def test_cookies_secure_flag_session_escape_and_audit_redact(self):
        self.assertIn("; Secure", session_cookie("tok", 60, secure=True))
        self.assertNotIn("Secure", session_cookie("tok", 60, secure=False))
        self.assertIn("; Secure", clear_cookie(secure=True))

        redacted = summarize_body({
            "url": "https://secret/?code=abc",
            "sp_dc": "cookie_val",
            "refresh_token": "rt",
            "artist": "Drake",
        })
        self.assertNotIn("cookie_val", redacted)
        self.assertNotIn("https://secret", redacted)
        self.assertIn("Drake", redacted)

        with Auth(self.db_path) as a:
            a.create_first_admin("alice", "correct-horse-99")
            p, _tok, _ = a.authenticate("alice", "correct-horse-99")
            # Underscore/percent wildcard in prefix must not match unrelated sessions
            self.assertEqual(a.revoke_session("________", user_id=p.id), 0)
            self.assertEqual(a.revoke_session(p.session_id[:12], user_id=p.id), 1)


if __name__ == "__main__":
    unittest.main()

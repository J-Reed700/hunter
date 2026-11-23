"""End-to-end HTTP security & protocol tests for hunter.web.{router,common,server,routes.auth}."""
from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import auth as authmod
from hunter.auth import Auth
from hunter.config import HunterConfig, clear_config_cache
from hunter.db import reset_schema_cache
from hunter.web import common, router, server as server_mod
from hunter.web.server import STATIC_DIR, Ctx, start_ui


def _http_req(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        hdrs = {"Host": f"127.0.0.1:{port}"}
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        resp_hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_hdrs, data
    finally:
        conn.close()


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        reset_schema_cache()
        clear_config_cache()
        with authmod._fails_lock:
            authmod._fails.clear()

        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "HUNTER_STATE_DIR": str(self.state_dir),
                "HUNTER_UI_TOKEN": "top-secret-api-token",
                "HUNTER_TRUSTED_PROXIES": "127.0.0.1/32,10.0.0.0/8",
            },
            clear=False,
        )
        self.env_patch.start()
        self.cfg = HunterConfig.from_env()
        self.server = start_ui(self.cfg, host="127.0.0.1", port=0)
        assert self.server is not None
        self.port = self.server.server_address[1]

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self.env_patch.stop()
        clear_config_cache()
        reset_schema_cache()
        with authmod._fails_lock:
            authmod._fails.clear()
        self.tmp.cleanup()

    def _setup_admin(self, username: str = "admin", password: str = "correct-horse-99") -> str:
        status, hdrs, data = _http_req(
            self.port,
            "POST",
            "/api/auth/setup",
            body=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200, data)
        cookie = hdrs["set-cookie"].split(";", 1)[0]
        return cookie

    def test_atomic_setup_race(self):
        # Pre-init DB schema
        Auth(self.cfg.state_db).close()
        results: list[int] = []
        lock = threading.Lock()

        def try_setup(i: int):
            st, _h, _d = _http_req(
                self.port,
                "POST",
                "/api/auth/setup",
                body=json.dumps({"username": f"admin{i}", "password": "correct-horse-99"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with lock:
                results.append(st)

        threads = [threading.Thread(target=try_setup, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 3)

    def test_trusted_proxy_xff_rightmost_untrusted_and_untrusted_peer(self):
        self._setup_admin()
        # Trusted peer (127.0.0.1) with spoofed leftmost IP and internal proxy hop:
        # must select rightmost non-trusted IP (203.0.113.55), NOT 1.2.3.4!
        status, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "admin", "password": "wrong-password-1"}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-For": "1.2.3.4, 203.0.113.55, 10.0.0.5",
            },
        )
        self.assertEqual(status, 401)
        with Auth(self.cfg.state_db) as a:
            row = a.audit_rows(action="auth.login", limit=1)["rows"][0]
            self.assertEqual(row["ip"], "203.0.113.55")

        # Untrusted peer: clear trusted_proxies so 127.0.0.1 is NOT trusted -> ignores XFF & XFP
        with mock.patch.dict(os.environ, {"HUNTER_TRUSTED_PROXIES": "192.0.2.0/24"}):
            clear_config_cache()
            st2, h2, _d2 = _http_req(
                self.port,
                "POST",
                "/api/auth/login",
                body=json.dumps({"username": "admin", "password": "correct-horse-99"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Forwarded-For": "8.8.8.8",
                    "X-Forwarded-Proto": "https",
                },
            )
            self.assertEqual(st2, 200)
            self.assertNotIn("strict-transport-security", h2)
            self.assertNotIn("; Secure", h2["set-cookie"])

    def test_https_proxy_sets_secure_cookie_and_hsts(self):
        status, hdrs, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/setup",
            body=json.dumps({"username": "admin", "password": "correct-horse-99"}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Forwarded-Proto": "https",
                "Origin": f"https://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("; Secure", hdrs["set-cookie"])
        self.assertIn("max-age=31536000", hdrs["strict-transport-security"])

    def test_csrf_protections(self):
        cookie = self._setup_admin()

        # 1. Cookie-authenticated POST missing Origin & Sec-Fetch-Site -> 403
        st1, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/logout_all",
            body=b"{}",
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        self.assertEqual(st1, 403)

        # 2. http -> https Origin downgrade -> 403
        st2, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/logout_all",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "X-Forwarded-Proto": "https",
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(st2, 403)

        # 3. text/plain simple cross-origin POST -> 415
        st3, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/logout_all",
            body=b"{}",
            headers={
                "Content-Type": "text/plain",
                "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(st3, 415)

        # 4. Login CSRF (foreign Origin or Sec-Fetch-Site: cross-site) -> 403
        st4, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "admin", "password": "correct-horse-99"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Origin": "http://evil.example",
            },
        )
        self.assertEqual(st4, 403)
        st5, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "admin", "password": "correct-horse-99"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        self.assertEqual(st5, 403)

        # 5. Valid same-origin cookie POST succeeds
        st6, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/logout_all",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(st6, 200)

    def test_must_change_pw_blocks_get_and_post_except_allowlist(self):
        admin_cookie = self._setup_admin()
        st, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/users/create",
            body=json.dumps({
                "username": "op1",
                "password": "temp-password-99",
                "role": "operator",
                "must_change_pw": True,
            }).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": admin_cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(st, 200)

        st_l, h_l, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "op1", "password": "temp-password-99"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(st_l, 200)
        op_cookie = h_l["set-cookie"].split(";", 1)[0]

        # Non-auth GET blocked with 403 must_change_pw
        st_g, _h, d_g = _http_req(
            self.port, "GET", "/api/auth/sessions", headers={"Cookie": op_cookie}
        )
        self.assertEqual(st_g, 403)
        self.assertTrue(json.loads(d_g)["must_change_pw"])

        # /api/auth/me and /healthz still allowed
        self.assertEqual(
            _http_req(self.port, "GET", "/api/auth/me", headers={"Cookie": op_cookie})[0],
            200,
        )
        self.assertEqual(_http_req(self.port, "GET", "/healthz")[0], 200)

        # Operator cannot access /api/audit even after changing password
        st_pw, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/password",
            body=json.dumps({"current": "temp-password-99", "new": "new-password-99"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Cookie": op_cookie,
                "Origin": f"http://127.0.0.1:{self.port}",
            },
        )
        self.assertEqual(st_pw, 200)
        self.assertEqual(
            _http_req(self.port, "GET", "/api/audit", headers={"Cookie": op_cookie})[0],
            403,
        )
        self.assertEqual(
            _http_req(self.port, "GET", "/api/audit", headers={"Cookie": admin_cookie})[0],
            200,
        )

    def test_password_whitespace_preservation(self):
        # Leading/trailing whitespace is rejected by policy on setup/create
        st_bad, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/setup",
            body=json.dumps({"username": "admin", "password": "  padded-pw  "}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(st_bad, 400)

        # Internal spaces are preserved verbatim (not stripped/altered)
        self._setup_admin("admin", "pass with spaces")
        st_ok, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/login",
            body=json.dumps({"username": "admin", "password": "pass with spaces"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(st_ok, 200)

    def test_content_length_invalid_and_overflow_close_connection(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(
                b"POST /api/auth/login HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: notanumber\r\n\r\n"
            )
            raw = s.recv(4096)
            self.assertIn(b"400", raw)
            self.assertIn(b"Connection: close", raw)

        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            s.sendall(
                b"POST /api/auth/login HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 2000000\r\n\r\n"
            )
            raw = s.recv(4096)
            self.assertIn(b"413", raw)
            self.assertIn(b"Connection: close", raw)

    def test_stream_file_range_200_206_416(self):
        p = Path(self.tmp.name) / "sample.bin"
        p.write_bytes(b"0123456789")

        r200 = Ctx.stream_file(p, "sample.bin", "")
        self.assertEqual(r200.code, 200)
        self.assertEqual(b"".join(r200.stream), b"0123456789")

        r206 = Ctx.stream_file(p, "sample.bin", "bytes=2-5")
        self.assertEqual(r206.code, 206)
        self.assertEqual(r206.headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(b"".join(r206.stream), b"2345")

        r_suffix = Ctx.stream_file(p, "sample.bin", "bytes=-3")
        self.assertEqual(r_suffix.code, 206)
        self.assertEqual(b"".join(r_suffix.stream), b"789")

        for bad in ("bytes=99-100", "bytes=0-1,2-3", "bytes=abc-def", "items=0-5", "bytes=-"):
            r416 = Ctx.stream_file(p, "sample.bin", bad)
            self.assertEqual(r416.code, 416, bad)
            self.assertEqual(r416.headers["Content-Range"], "bytes */10")

    def test_static_traversal_method_405_options_token_lockout_and_bb(self):
        self._setup_admin()

        # Static traversal blocked
        self.assertEqual(_http_req(self.port, "GET", "/static/../../hunter/__init__.py")[0], 404)
        self.assertEqual(_http_req(self.port, "GET", "/static/index.html")[0], 200)

        # 405 Method Not Allowed & OPTIONS
        st_405, hdrs_405, _d = _http_req(self.port, "DELETE", "/api/auth/me")
        self.assertEqual(st_405, 405)
        self.assertIn("GET", hdrs_405["allow"])
        st_opt, hdrs_opt, _d = _http_req(self.port, "OPTIONS", "/api/auth/me")
        self.assertEqual(st_opt, 204)
        self.assertIn("GET", hdrs_opt["allow"])

        # Token query param NOT accepted on POST, and header token triggers lockout after failures
        st_qtok, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/logout_all?token=top-secret-api-token",
            body=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(st_qtok, 401)

        for _ in range(authmod.LOCKOUT_AFTER):
            _http_req(
                self.port,
                "GET",
                "/api/audit",
                headers={"X-Hunter-Token": "wrong-token"},
            )
        st_locked, _h, _d = _http_req(
            self.port,
            "GET",
            "/api/audit",
            headers={"X-Hunter-Token": "top-secret-api-token"},
        )
        self.assertEqual(st_locked, 429)

        # Ctx.bb handles string "false" / "0" cleanly
        ctx = Ctx(
            mock.Mock(),
            self.cfg,
            "POST",
            "/",
            {},
            {"a": "false", "b": "0", "c": "no", "d": "true", "e": True},
        )
        self.assertFalse(ctx.bb("a"))
        self.assertFalse(ctx.bb("b"))
        self.assertFalse(ctx.bb("c"))
        self.assertTrue(ctx.bb("d"))
        self.assertTrue(ctx.bb("e"))

    def test_router_stacking_does_not_clobber_permissions(self):
        # Verify perm_of resolves independently per registered entry
        self.assertEqual(router.resolve("GET", "/api/audit")[1].role, "admin")  # type: ignore[index]
        self.assertEqual(router.resolve("GET", "/api/auth/me")[1].role, "public")  # type: ignore[index]

    def test_common_reap_children_and_user_agent(self):
        self.assertNotIn("jreed", common.ART_SESSION.headers["User-Agent"])
        proc = mock.Mock()
        proc.poll.return_value = 0
        with common._children_lock:
            common._children.add(proc)
        self.assertGreaterEqual(common.reap_children(), 1)

    def test_http11_framing_desync_proxy_csrf_private_ip_and_nested_redact(self):
        cookie = self._setup_admin()
        import socket

        # 415 non-JSON POST must close connection
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(
                b"POST /api/auth/login HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: text/plain\r\n"
                b"Content-Length: 5\r\n\r\nhello"
            )
            raw = s.recv(4096)
            self.assertIn(b"415", raw)
            self.assertIn(b"Connection: close", raw)

        # Duplicate Content-Length -> 400 + Connection: close
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(
                b"POST /api/auth/login HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"Content-Length: 2\r\n\r\n{}"
            )
            raw = s.recv(4096)
            self.assertIn(b"400", raw)
            self.assertIn(b"Connection: close", raw)

        # Transfer-Encoding -> 411 + Connection: close
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(
                b"POST /api/auth/login HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            )
            raw = s.recv(4096)
            self.assertIn(b"411", raw)
            self.assertIn(b"Connection: close", raw)

        # GET with non-zero Content-Length -> 400 + Connection: close
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as s:
            s.sendall(
                b"GET /api/auth/me HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Content-Length: 4\r\n\r\nabcd"
            )
            raw = s.recv(4096)
            self.assertIn(b"400", raw)
            self.assertIn(b"Connection: close", raw)

        # X-Forwarded-Host + :443 default port normalization for reverse HTTPS proxy
        st_xfh, _h, _d = _http_req(
            self.port,
            "POST",
            "/api/auth/logout_all",
            body=b"{}",
            headers={
                "Content-Type": "application/json",
                "Cookie": cookie,
                "Host": "hunter:8080",
                "X-Forwarded-Host": "hunter.example.com:443",
                "X-Forwarded-Proto": "https",
                "Origin": "https://hunter.example.com",
            },
        )
        self.assertEqual(st_xfh, 200)

        # All-private X-Forwarded-For hops fall back to actual peer IP, not spoofed leftmost private hop
        handler = object.__new__(server_mod._Handler)
        handler.client_address = ("172.18.0.5", 12345)
        handler.headers = {"X-Forwarded-For": "10.99.99.99, 172.18.0.2"}
        self.assertEqual(handler._client_ip(self.cfg), "172.18.0.5")

        # Nested secret redaction in audit body summary
        summary = authmod.summarize_body({"nested": {"api_token": "secret123", "ok": 1}})
        self.assertNotIn("secret123", summary)
        self.assertIn("•••", summary)


if __name__ == "__main__":
    unittest.main()

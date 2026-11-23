"""HTTP server + request context. stdlib only, threaded, bounded concurrency.

Hardened for internet exposure behind a reverse proxy:
  * 30s socket timeout + bounded concurrency semaphore (64) + queue size 32
  * HTTP/1.1 keep-alive safety (close_connection on invalid/oversize body or
    truncated stream)
  * Trusted-proxy-aware right-to-left X-Forwarded-For + HTTPS / HSTS
  * Constant-time HUNTER_UI_TOKEN with IP lockout and query-token log redaction
  * Strict CSRF validation (Sec-Fetch-Site, scheme+host Origin, JSON Content-Type,
    fail-closed on cookie POSTs missing origin headers, login/setup covered)
  * must_change_pw blocks ALL non-auth GET & POST routes
  * Symlink-confined static file serving + RFC-compliant Range 200/206/416
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from hunter import auth as authmod, domain
from hunter.auth import Auth, Principal
from hunter.config import HunterConfig
from hunter.web import router

log = logging.getLogger("hunter.webui")

STATIC_DIR = Path(__file__).parent / "static"
_ETAGS: dict[str, tuple[int, str]] = {}
_ETAGS_LOCK = threading.Lock()

_MAX_BODY_BYTES = 1_048_576
_SOCKET_TIMEOUT_S = 30.0
_MAX_INFLIGHT = 64
_MUST_CHANGE_ALLOW = frozenset({
    "/api/auth/me",
    "/api/auth/password",
    "/api/auth/logout",
})
# Only calendar feeds and browser-initiated file downloads may authenticate via ?token=
_QUERY_TOKEN_PATHS = frozenset({
    "/calendar.ics",
    "/api/downloads/file",
    "/api/dl/track",
})
_TOKEN_REDACT_RE = re.compile(r"([?&]token=)[^&\s]*", re.IGNORECASE)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: https:; "
        "style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; base-uri 'self'"
    ),
}


def _sanitize_path(raw: str) -> str:
    return _TOKEN_REDACT_RE.sub(r"\1***", raw or "")


def _norm_host(netloc: str, scheme: str) -> str:
    h = (netloc or "").strip().lower()
    if not h:
        return ""
    default_port = ":443" if scheme == "https" else ":80" if scheme == "http" else ""
    if default_port and h.endswith(default_port):
        prefix = h[: -len(default_port)]
        if prefix and (":" not in prefix or prefix.endswith("]")):
            return prefix
    return h


def _parse_ip(raw: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address((raw or "").strip())
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _parse_trusted_nets(
    cidrs: tuple[str, ...],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw in cidrs:
        try:
            nets.append(ipaddress.ip_network(raw.strip(), strict=False))
        except ValueError:
            continue
    return nets


def _ip_in_nets(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    if ip is None:
        return False
    return any(ip in net for net in nets)


class Response:
    __slots__ = ("code", "body", "ctype", "headers", "stream", "length")

    def __init__(
        self,
        code: int,
        body: bytes,
        ctype: str,
        headers: dict[str, str] | None = None,
        stream=None,
        length: int = 0,
    ):
        self.code, self.body, self.ctype = code, body, ctype
        self.headers = headers or {}
        self.stream, self.length = stream, length


def _content_disposition(filename: str) -> str:
    safe = (
        "".join(
            c if 32 <= ord(c) < 127 and c not in '"\\' else "_"
            for c in filename
        )
        or "download"
    )
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(filename)}"


class Ctx:
    def __init__(
        self,
        handler: "_Handler",
        cfg: HunterConfig,
        method: str,
        path: str,
        query: dict[str, list[str]],
        body: dict[str, Any],
        user: Principal | None = None,
        ip: str = "",
        is_https: bool = False,
    ):
        self.handler = handler
        self.cfg = cfg
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.user = user
        self.ip = ip
        self.is_https = is_https

    # ---- input --------------------------------------------------------------

    def q(self, key: str, default: str = "") -> str:
        return (self.query.get(key, [default])[0] or default).strip()

    def qi(
        self,
        key: str,
        default: int = 0,
        lo: int | None = None,
        hi: int | None = None,
    ) -> int:
        try:
            v = int(self.query.get(key, [str(default)])[0] or default)
        except (TypeError, ValueError):
            v = default
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return v

    def b(self, key: str, default: str = "") -> str:
        v = self.body.get(key, default)
        return (str(v) if v is not None else default).strip()

    def bi(self, key: str, default: int = 0) -> int:
        v = self.body.get(key)
        if isinstance(v, bool) or v is None or v == "":
            return default
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def bb(self, key: str, default: bool = False) -> bool:
        v = self.body.get(key)
        if v is None:
            return default
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "yes", "on"):
                return True
            if s in ("0", "false", "no", "off", ""):
                return False
            return default
        return bool(v)

    def blist(self, key: str, kind=str, limit: int = 5000) -> list:
        v = self.body.get(key)
        if not isinstance(v, list):
            return []
        out = []
        for x in v[:limit]:
            try:
                out.append(kind(x))
            except (TypeError, ValueError):
                continue
        return out

    @property
    def header(self):
        return self.handler.headers

    @property
    def username(self) -> str:
        return self.user.username if self.user else "anonymous"

    def auth(self) -> Auth:
        return Auth(self.cfg.state_db)

    # ---- output -------------------------------------------------------------

    @staticmethod
    def json(
        code: int, obj: Any, headers: dict[str, str] | None = None
    ) -> Response:
        return Response(
            code,
            json.dumps(obj, default=_json_default).encode("utf-8"),
            "application/json",
            headers,
        )

    @staticmethod
    def send(
        code: int,
        body: bytes,
        ctype: str,
        headers: dict[str, str] | None = None,
    ) -> Response:
        return Response(code, body, ctype, headers)

    @staticmethod
    def error(code: int, msg: str, **extra: Any) -> Response:
        return Ctx.json(code, {"error": msg, **extra})

    @staticmethod
    def stream_file(
        path: Path,
        filename: str,
        range_header: str = "",
        ctype: str | None = None,
        chunk: int = 1 << 20,
    ) -> Response:
        """Stream a file from disk with strict RFC Range support (200/206/416)."""
        try:
            size = path.stat().st_size
        except OSError:
            return Ctx.error(404, "not found")

        start, end, code = 0, max(0, size - 1), 200
        rh = (range_header or "").strip()
        if rh:
            if not rh.lower().startswith("bytes="):
                return Response(
                    416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                )
            spec = rh[6:].strip()
            if "," in spec or "-" not in spec or size == 0:
                return Response(
                    416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                )
            a_raw, _, b_raw = spec.partition("-")
            a, b = a_raw.strip(), b_raw.strip()
            if not a and not b:
                return Response(
                    416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                )
            if not a:
                if not b.isdigit():
                    return Response(
                        416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                    )
                suffix = int(b)
                if suffix <= 0:
                    return Response(
                        416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                    )
                start = max(0, size - suffix)
                end = size - 1
            else:
                if not a.isdigit() or (b and not b.isdigit()):
                    return Response(
                        416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                    )
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
                if start >= size or start > end or (b and int(b) < start):
                    return Response(
                        416, b"", "text/plain", {"Content-Range": f"bytes */{size}"}
                    )
            code = 206

        length = 0 if size == 0 else (end - start + 1)

        def it():
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    data = f.read(min(chunk, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data

        hdrs = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": _content_disposition(filename),
            "Cache-Control": "no-store",
        }
        if code == 206:
            hdrs["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(
            code,
            b"",
            ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream",
            hdrs,
            stream=it(),
            length=length,
        )

    @staticmethod
    def file(
        path: Path, ctype: str | None = None, cache: str = "no-cache"
    ) -> Response:
        try:
            body = path.read_bytes()
        except OSError:
            return Ctx.error(404, "not found")
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        return Response(200, body, ctype, {"Cache-Control": cache})


def _json_default(o: Any) -> str:
    if isinstance(o, (bytes, bytearray)):
        return o.decode("utf-8", "replace")
    if isinstance(o, Path):
        return str(o)
    return str(o)


class _BodyError(Exception):
    def __init__(self, code: int, msg: str, close: bool = False):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.close = close


class _Handler(BaseHTTPRequestHandler):
    server_version = "hunter"
    protocol_version = "HTTP/1.1"
    timeout = _SOCKET_TIMEOUT_S

    def log_message(self, fmt: str, *args: Any) -> None:
        try:
            msg = fmt % args if args else fmt
        except Exception:                             # noqa: BLE001
            msg = f"{fmt} {args!r}"
        log.debug("ui: %s", _sanitize_path(msg))

    # ---- identity / proxy --------------------------------------------------

    def _peer_trusted(self, cfg: HunterConfig) -> tuple[str, bool]:
        peer_raw = (self.client_address[0] if self.client_address else "").strip()
        peer_ip = _parse_ip(peer_raw)
        nets = _parse_trusted_nets(getattr(cfg, "trusted_proxies", ()))
        return peer_raw, _ip_in_nets(peer_ip, nets)

    def _client_ip(self, cfg: HunterConfig) -> str:
        peer_raw, trusted = self._peer_trusted(cfg)
        if not trusted:
            return peer_raw[:64]
        fwd = self.headers.get("X-Forwarded-For", "")
        if not fwd:
            return peer_raw[:64]
        nets = _parse_trusted_nets(getattr(cfg, "trusted_proxies", ()))
        hops = [h.strip() for h in fwd.split(",") if h.strip()]
        for hop in reversed(hops):
            parsed = _parse_ip(hop)
            if parsed is not None and not _ip_in_nets(parsed, nets):
                return str(parsed)[:64]
        return peer_raw[:64]

    def _is_https(self, cfg: HunterConfig) -> bool:
        if isinstance(getattr(self, "connection", None), ssl.SSLSocket):
            return True
        _peer_raw, trusted = self._peer_trusted(cfg)
        if not trusted:
            return False
        xfp = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return xfp == "https"

    def _token_supplied(
        self, method: str, path: str, query: dict[str, list[str]]
    ) -> str:
        supplied = self.headers.get("X-Hunter-Token", "")
        if supplied:
            return supplied
        if method in ("GET", "HEAD") and path in _QUERY_TOKEN_PATHS:
            return query.get("token", [""])[0]
        return ""

    def _principal(
        self,
        cfg: HunterConfig,
        method: str,
        path: str,
        query: dict[str, list[str]],
        ip: str,
        auth: Auth,
    ) -> tuple[Principal | None, bool]:
        """Returns ``(principal, token_locked_out)``."""
        tok = self._token_supplied(method, path, query)
        if tok:
            if authmod.check_token_lockout(ip):
                return None, True
            if cfg.ui_token and hmac.compare_digest(
                tok.encode("utf-8"), cfg.ui_token.encode("utf-8")
            ):
                authmod.clear_token_failure(ip)
                return authmod.token_principal(), False
            authmod.record_token_failure(ip)
            return None, False

        cookies = authmod.parse_cookies(self.headers.get("Cookie", ""))
        raw = cookies.get(authmod.SESSION_COOKIE, "")
        if raw:
            return auth.principal_for_token(raw, ip=ip), False
        return None, False

    def _same_origin(
        self,
        *,
        is_https: bool,
        trusted_peer: bool = False,
        allow_absent_headers: bool = False,
    ) -> bool:
        expected_scheme = "https" if is_https else "http"
        raw_host = ""
        if trusted_peer:
            xfh = (self.headers.get("X-Forwarded-Host") or "").split(",", 1)[0].strip()
            if xfh:
                raw_host = xfh
        if not raw_host:
            raw_host = (self.headers.get("Host") or "").strip()
        host = _norm_host(raw_host, expected_scheme)

        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs and sfs not in ("same-origin", "none"):
            return False

        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            parsed = urlparse(origin)
            ps = parsed.scheme.lower()
            return (
                ps == expected_scheme
                and bool(host)
                and _norm_host(parsed.netloc, ps) == host
            )

        referer = (self.headers.get("Referer") or "").strip()
        if referer:
            parsed = urlparse(referer)
            ps = parsed.scheme.lower()
            return (
                ps == expected_scheme
                and bool(host)
                and _norm_host(parsed.netloc, ps) == host
            )

        if sfs in ("same-origin", "none"):
            return True
        return allow_absent_headers

    # ---- plumbing ----------------------------------------------------------

    def _write(self, resp: Response, *, is_https: bool = False) -> None:
        self.send_response(resp.code)
        self.send_header("Content-Type", resp.ctype)
        self.send_header(
            "Content-Length",
            str(resp.length if resp.stream is not None else len(resp.body)),
        )
        if "Cache-Control" not in resp.headers:
            self.send_header("Cache-Control", "no-store")
        for k, v in _SECURITY_HEADERS.items():
            self.send_header(k, v)
        if is_https:
            self.send_header(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        for k, v in resp.headers.items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            close = getattr(resp.stream, "close", None)
            if close:
                close()
            return
        if resp.stream is None:
            self.wfile.write(resp.body)
            return
        sent = 0
        try:
            for chunk in resp.stream:
                self.wfile.write(chunk)
                sent += len(chunk)
            if sent != resp.length:
                self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True
        finally:
            close = getattr(resp.stream, "close", None)
            if close:
                close()

    def _validate_framing(self, method: str) -> int:
        te_vals = [
            v.strip() for v in self.headers.get_all("Transfer-Encoding", []) if v.strip()
        ]
        if te_vals:
            raise _BodyError(411, "Transfer-Encoding not supported", close=True)

        cl_vals = [v.strip() for v in self.headers.get_all("Content-Length", [])]
        if len(cl_vals) > 1:
            raise _BodyError(400, "multiple Content-Length headers", close=True)
        if not cl_vals or not cl_vals[0]:
            return 0
        try:
            length = int(cl_vals[0])
        except ValueError as exc:
            raise _BodyError(400, "invalid Content-Length", close=True) from exc
        if length < 0:
            raise _BodyError(400, "invalid Content-Length", close=True)
        if method not in ("POST", "PUT", "PATCH"):
            if length > 0:
                raise _BodyError(400, f"{method} body not supported", close=True)
            return 0
        if length > _MAX_BODY_BYTES:
            raise _BodyError(413, "request body too large", close=True)
        return length

    def _read_body(self, method: str) -> dict[str, Any]:
        length = self._validate_framing(method)
        if length == 0:
            return {}

        if method == "POST":
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                raise _BodyError(
                    415, "Content-Type must be application/json", close=True
                )

        raw = self.rfile.read(length)
        if len(raw) != length:
            raise _BodyError(400, "incomplete request body", close=True)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _BodyError(400, "invalid JSON body") from exc
        return data if isinstance(data, dict) else {"_": data}

    def _handle(self, method: str) -> None:
        cfg = HunterConfig.from_env()
        is_https = False
        try:
            is_https = self._is_https(cfg)
            _peer_raw, trusted_peer = self._peer_trusted(cfg)
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            body = self._read_body(method)

            is_static_path = (
                path == "/healthz"
                or path == "/"
                or path == "/index.html"
                or path.startswith("/static/")
            )
            if method == "OPTIONS":
                allowed = router.allowed_methods(path)
                if is_static_path:
                    allowed = sorted(set(allowed) | {"GET", "HEAD"})
                if not allowed:
                    return self._write(Ctx.error(404, "not found"), is_https=is_https)
                return self._write(
                    Response(204, b"", "text/plain", {"Allow": ", ".join(allowed)}),
                    is_https=is_https,
                )
            if is_static_path:
                if method != "GET":
                    return self._write(
                        Ctx.json(
                            405,
                            {"error": "method not allowed"},
                            {"Allow": "GET, HEAD"},
                        ),
                        is_https=is_https,
                    )
                if path == "/healthz":
                    return self._write(Ctx.json(200, {"ok": True}), is_https=is_https)
                if path == "/" or path == "/index.html":
                    return self._write(
                        self._static("index.html", shell=True), is_https=is_https
                    )
                return self._write(
                    self._static(path[len("/static/"):]), is_https=is_https
                )

            hit = router.resolve(method, path)
            if hit is None:
                allowed = router.allowed_methods(path)
                if allowed:
                    return self._write(
                        Ctx.json(
                            405,
                            {"error": "method not allowed"},
                            {"Allow": ", ".join(allowed)},
                        ),
                        is_https=is_https,
                    )
                return self._write(Ctx.error(404, "not found"), is_https=is_https)
            fn, perm = hit
            ip = self._client_ip(cfg)
            with Auth(cfg.state_db) as auth:
                user, tok_locked = self._principal(cfg, method, path, query, ip, auth)
                if tok_locked:
                    return self._write(
                        Ctx.error(429, "too many invalid token attempts"),
                        is_https=is_https,
                    )
                setup = auth.setup_required
                has_cookie = bool(
                    authmod.parse_cookies(self.headers.get("Cookie", "")).get(
                        authmod.SESSION_COOKIE
                    )
                )
                if method == "POST" and (
                    (user is not None and user.via == "cookie")
                    or path in ("/api/auth/login", "/api/auth/setup")
                ):
                    allow_absent = (
                        user is None
                        and not has_cookie
                        and path in ("/api/auth/login", "/api/auth/setup")
                    )
                    if not self._same_origin(
                        is_https=is_https,
                        trusted_peer=trusted_peer,
                        allow_absent_headers=allow_absent,
                    ):
                        auth.audit(
                            user,
                            f"{method} {path}",
                            "",
                            "cross-origin refused",
                            ip,
                            ok=False,
                        )
                        return self._write(
                            Ctx.error(403, "cross-origin request refused"),
                            is_https=is_https,
                        )
                if not perm.is_public:
                    if setup:
                        return self._write(
                            Ctx.json(
                                401,
                                {"error": "setup required", "setup_required": True},
                            ),
                            is_https=is_https,
                        )
                    if user is None:
                        return self._write(
                            Ctx.json(
                                401,
                                {"error": "sign in required", "setup_required": False},
                            ),
                            is_https=is_https,
                        )
                    if not user.can(perm.role):
                        auth.audit(
                            user,
                            f"{method} {path}",
                            authmod.target_of(body),
                            f"needs {perm.role}",
                            ip,
                            ok=False,
                        )
                        return self._write(
                            Ctx.error(
                                403,
                                f"this action needs the {perm.role} role"
                                f" (you are {user.role})",
                                role=user.role,
                                needs=perm.role,
                            ),
                            is_https=is_https,
                        )
                    if user.must_change_pw and path not in _MUST_CHANGE_ALLOW:
                        return self._write(
                            Ctx.error(
                                403,
                                "password change required",
                                must_change_pw=True,
                            ),
                            is_https=is_https,
                        )

            ctx = Ctx(
                self,
                cfg,
                method,
                path,
                query,
                body,
                user,
                ip,
                is_https=is_https,
            )
            resp = fn(ctx)
            if not isinstance(resp, Response):
                resp = Ctx.json(
                    200, resp if resp is not None else {"ok": True}
                )
            if perm.audit and 200 <= resp.code < 300 and user is not None:
                with Auth(cfg.state_db) as auth:
                    auth.audit(
                        user,
                        f"{method} {path}",
                        authmod.target_of(body),
                        authmod.summarize_body(body),
                        ip,
                        ok=True,
                    )
            self._write(resp, is_https=is_https)
        except _BodyError as exc:
            if exc.close:
                self.close_connection = True
            self._write(Ctx.error(exc.code, exc.msg), is_https=is_https)
        except Exception:                             # noqa: BLE001
            log.exception("%s %s failed", method, _sanitize_path(self.path))
            try:
                self._write(
                    Ctx.error(500, "internal server error"),
                    is_https=is_https,
                )
            except Exception:                         # noqa: BLE001
                self.close_connection = True

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    # ---- static -------------------------------------------------------------

    def _static(self, rel: str, shell: bool = False) -> Response:
        if "\x00" in rel:
            return Ctx.error(404, "not found")
        rel_norm = os.path.normpath(rel).lstrip("/")
        if rel_norm.startswith(".."):
            return Ctx.error(404, "not found")
        p = domain.resolve_under(STATIC_DIR, STATIC_DIR / rel_norm)
        if p is None or not p.is_file():
            return Ctx.error(404, "not found")
        try:
            st = p.stat()
        except OSError:
            return Ctx.error(404, "not found")
        ver = ""
        if shell:
            from hunter import __version__

            ver = __version__
        etag_key = f"{rel_norm}:shell:{ver}" if shell else rel_norm
        with _ETAGS_LOCK:
            cached = _ETAGS.get(etag_key)
            if cached and cached[0] == st.st_mtime_ns:
                etag = cached[1]
            else:
                etag = (
                    '"'
                    + hashlib.md5(
                        f"{st.st_mtime_ns}:{st.st_size}:{ver}".encode()
                    ).hexdigest()[:16]
                    + '"'
                )
                _ETAGS[etag_key] = (st.st_mtime_ns, etag)
        if self.headers.get("If-None-Match") == etag:
            return Response(
                304, b"", "text/plain", {"ETag": etag, "Cache-Control": "no-cache"}
            )
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if p.suffix == ".js":
            ctype = "text/javascript"
        body = p.read_bytes()
        if shell:
            body = body.replace(b"{{VERSION}}", ver.encode())
        return Response(
            200,
            body,
            f"{ctype}; charset=utf-8" if ctype.startswith("text/") else ctype,
            {"ETag": etag, "Cache-Control": "no-cache"},
        )


class _Server(ThreadingHTTPServer):
    request_queue_size = 32
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], RequestHandlerClass):
        self._sem = threading.BoundedSemaphore(_MAX_INFLIGHT)
        super().__init__(server_address, RequestHandlerClass)

    def process_request(self, request, client_address) -> None:
        if not self._sem.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 29\r\n"
                    b"Connection: close\r\n\r\n"
                    b'{"error":"server overloaded"}'
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._sem.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._sem.release()


def start_ui(
    cfg: HunterConfig, *, host: str = "0.0.0.0", port: int | None = None
) -> ThreadingHTTPServer | None:
    """Launch the UI server on a daemon thread and return its handle."""
    bind_port = cfg.ui_port if port is None else port
    if bind_port is None or (port is None and not cfg.ui_port):
        return None
    from hunter.web import routes  # noqa: F401
    try:
        server = _Server((host, bind_port), _Handler)
    except OSError as exc:
        log.error("webui failed to bind :%d — %s (UI disabled)", bind_port, exc)
        return None
    threading.Thread(
        target=server.serve_forever, name="webui", daemon=True
    ).start()
    try:
        with Auth(cfg.state_db) as a:
            n_users = a.user_count()
    except Exception as exc:                          # noqa: BLE001
        log.warning("auth store unavailable: %s", exc)
        n_users = -1
    actual_port = server.server_address[1]
    log.info(
        "webui listening on :%d (%d routes, %d users, api token %s)",
        actual_port,
        len(router.routes()),
        n_users,
        "set" if cfg.ui_token else "not set",
    )
    if n_users == 0:
        log.warning(
            "no users yet — the UI is in SETUP mode: the first visitor "
            "on port %d creates the admin account",
            actual_port,
        )
    return server


__all__ = ["Ctx", "Response", "STATIC_DIR", "start_ui"]

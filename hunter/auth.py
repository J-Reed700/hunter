"""Authentication + authorization for the hunter console.

Layer 2 — uses ``hunter.db.open_hunter_db`` and ``hunter.domain``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hunter import domain
from hunter.db import open_hunter_db

log = logging.getLogger("hunter.auth")

ROLES = ("viewer", "operator", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_COOKIE = "hunter_session"
SESSION_TTL = 12 * 3600            # plain login
SESSION_TTL_REMEMBER = 30 * 86_400  # "remember me"
SESSION_TOUCH_EVERY = 300           # write last_seen at most this often

MIN_PASSWORD = 8
LOCKOUT_AFTER = 6
LOCKOUT_SECONDS = 300
FAIL_DELAY = 0.3
_MAX_THROTTLE_ENTRIES = 10_000

_REDACT = (
    "password", "passwd", "new", "current", "secret", "client_secret",
    "sp_dc", "token", "api_key", "cookie", "url", "code", "refresh_token",
)

_AUDIT_KEEP = 20_000


class AuthError(Exception):
    """User-facing auth failure (message is safe to return)."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class Principal:
    """Who is making the request. ``via`` is cookie | token."""
    id: int
    username: str
    display: str
    role: str
    via: str = "cookie"
    session_id: str = ""
    must_change_pw: bool = False

    def can(self, role: str) -> bool:
        return _RANK.get(self.role, -1) >= _RANK.get(role, 99)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display": self.display,
            "role": self.role,
            "via": self.via,
            "must_change_pw": self.must_change_pw,
        }


# --------------------------------------------------------------------------
# password hashing
# --------------------------------------------------------------------------

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=64 * 1024 * 1024,
            dklen=32,
        )
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


_DUMMY_PW_HASH: str | None = None
_DUMMY_LOCK = threading.Lock()


def _dummy_hash() -> str:
    global _DUMMY_PW_HASH
    with _DUMMY_LOCK:
        if _DUMMY_PW_HASH is None:
            _DUMMY_PW_HASH = hash_password("hunter-constant-time-dummy")
        return _DUMMY_PW_HASH


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
    if len(password) > 256:
        raise AuthError("password too long")
    if password.strip() != password:
        raise AuthError("password must not start or end with whitespace")


def check_username(username: str) -> str:
    u = (username or "").strip()
    if not (2 <= len(u) <= 32):
        raise AuthError("username must be 2–32 characters")
    if not all(c.isalnum() or c in "._-" for c in u):
        raise AuthError("username may contain letters, digits, . _ -")
    if u.lower() in ("token", "system", "anonymous"):
        raise AuthError("reserved username")
    return u


# --------------------------------------------------------------------------
# bounded LRU brute-force throttle (evicts expired/oldest; never clears all)
# --------------------------------------------------------------------------

_fails: OrderedDict[str, tuple[int, float]] = OrderedDict()
_fails_lock = threading.Lock()


def _throttle_key(ip: str, subject: str) -> str:
    return f"{ip}|{subject.lower()}"


def _evict_if_full(now: float) -> None:
    if len(_fails) < _MAX_THROTTLE_ENTRIES:
        return
    expired = [k for k, (n, until) in _fails.items() if n >= LOCKOUT_AFTER and until <= now]
    for k in expired:
        _fails.pop(k, None)
    if len(_fails) < _MAX_THROTTLE_ENTRIES:
        return
    non_locked = [k for k, (n, until) in _fails.items() if n < LOCKOUT_AFTER or until <= now]
    for k in non_locked:
        _fails.pop(k, None)
        if len(_fails) < _MAX_THROTTLE_ENTRIES:
            return
    while len(_fails) >= _MAX_THROTTLE_ENTRIES:
        _fails.popitem(last=False)


def locked_for(ip: str, username: str) -> int:
    key = _throttle_key(ip, username)
    now = time.time()
    with _fails_lock:
        rec = _fails.get(key)
        if not rec:
            return 0
        n, until = rec
        if n >= LOCKOUT_AFTER:
            if until > now:
                _fails.move_to_end(key)
                return max(1, int(until - now))
            _fails.pop(key, None)
        return 0


def _note_failure(ip: str, username: str) -> None:
    key = _throttle_key(ip, username)
    now = time.time()
    with _fails_lock:
        rec = _fails.get(key)
        if rec is None:
            _evict_if_full(now)
            n = 1
        else:
            old_n, old_until = rec
            n = 1 if (old_n >= LOCKOUT_AFTER and old_until <= now) else old_n + 1
        until = now + LOCKOUT_SECONDS if n >= LOCKOUT_AFTER else 0.0
        _fails[key] = (n, until)
        _fails.move_to_end(key)


def _clear_failures(ip: str, username: str) -> None:
    with _fails_lock:
        _fails.pop(_throttle_key(ip, username), None)


def check_token_lockout(ip: str) -> int:
    return locked_for(ip, "__ui_token__")


def record_token_failure(ip: str) -> None:
    _note_failure(ip, "__ui_token__")


def clear_token_failure(ip: str) -> None:
    _clear_failures(ip, "__ui_token__")


# --------------------------------------------------------------------------
# cookie helpers (string-level only)
# --------------------------------------------------------------------------

def parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            out[k] = v
    return out


def session_cookie(
    token: str, max_age: int | None, *, secure: bool = False
) -> str:
    base = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
    if secure:
        base += "; Secure"
    if max_age is not None:
        base += f"; Max-Age={int(max_age)}"
    return base


def clear_cookie(*, secure: bool = False) -> str:
    base = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    if secure:
        base += "; Secure"
    return base


def _tok_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class Auth:
    def __init__(self, db_path: Path):
        self._conn = open_hunter_db(db_path, timeout=15.0)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Auth":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- state ---------------------------------------------------------------

    def user_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    @property
    def setup_required(self) -> bool:
        return self.user_count() == 0

    # ---- users ---------------------------------------------------------------

    @staticmethod
    def _pub(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"],
            "username": r["username"],
            "display": r["display"],
            "role": r["role"],
            "created_ts": r["created_ts"],
            "updated_ts": r["updated_ts"],
            "last_login_ts": r["last_login_ts"],
            "disabled": bool(r["disabled"]),
            "must_change_pw": bool(r["must_change_pw"]),
            "created_by": r["created_by"],
        }

    def users(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT u.*, (SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id"
            " AND s.expires_ts > ?) AS sessions FROM users u"
            " ORDER BY role DESC, username COLLATE NOCASE",
            (int(time.time()),),
        ).fetchall()
        out = []
        for r in rows:
            d = self._pub(r)
            d["sessions"] = r["sessions"]
            out.append(d)
        return out

    def user(self, user_id: int) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._pub(r) if r else None

    def user_by_name(self, username: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)
        ).fetchone()
        return self._pub(r) if r else None

    def create_first_admin(
        self, username: str, password: str, display: str = ""
    ) -> dict[str, Any]:
        """Atomically create the initial admin user under BEGIN IMMEDIATE."""
        username = check_username(username)
        check_password_policy(password)
        pw_hash = hash_password(password)
        now = int(time.time())

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            if self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
                self._conn.rollback()
                raise AuthError("setup already completed", 409)
            cur = self._conn.execute(
                "INSERT INTO users (username, display, role, pw, created_ts,"
                " updated_ts, must_change_pw, created_by)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    username,
                    (display or "").strip()[:64],
                    "admin",
                    pw_hash,
                    now,
                    now,
                    0,
                    "setup",
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()
            raise AuthError("setup already completed", 409) from None
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        log.info("first admin created: %s", username)
        return self.user(cur.lastrowid)  # type: ignore[return-value]

    def create_user(
        self,
        username: str,
        password: str,
        role: str = "viewer",
        display: str = "",
        created_by: str = "",
        must_change_pw: bool = False,
    ) -> dict[str, Any]:
        username = check_username(username)
        check_password_policy(password)
        if role not in ROLES:
            raise AuthError(f"role must be one of {', '.join(ROLES)}")
        now = int(time.time())
        pw_hash = hash_password(password)
        try:
            cur = self._conn.execute(
                "INSERT INTO users (username, display, role, pw, created_ts,"
                " updated_ts, must_change_pw, created_by) VALUES (?,?,?,?,?,?,?,?)",
                (
                    username,
                    (display or "").strip()[:64],
                    role,
                    pw_hash,
                    now,
                    now,
                    int(must_change_pw),
                    created_by,
                ),
            )
        except sqlite3.IntegrityError:
            raise AuthError("username already exists", 409) from None
        self._conn.commit()
        log.info("user created: %s (%s) by %s", username, role, created_by or "setup")
        return self.user(cur.lastrowid)  # type: ignore[return-value]

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        display: str | None = None,
        disabled: bool | None = None,
    ) -> dict[str, Any]:
        if role is not None and role not in ROLES:
            raise AuthError("bad role")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            u = self.user(user_id)
            if not u:
                raise AuthError("unknown user", 404)
            sets: list[str] = []
            args: list[Any] = []
            if role is not None:
                if u["role"] == "admin" and role != "admin":
                    self._ensure_not_last_admin(user_id)
                sets.append("role=?")
                args.append(role)
            if display is not None:
                sets.append("display=?")
                args.append(display.strip()[:64])
            if disabled is not None:
                if disabled and u["role"] == "admin":
                    self._ensure_not_last_admin(user_id)
                sets.append("disabled=?")
                args.append(int(disabled))
            if not sets:
                self._conn.rollback()
                return u
            sets.append("updated_ts=?")
            args.append(int(time.time()))
            args.append(user_id)
            self._conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)
            if disabled:
                self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        return self.user(user_id)  # type: ignore[return-value]

    def delete_user(self, user_id: int) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            u = self.user(user_id)
            if not u:
                raise AuthError("unknown user", 404)
            if u["role"] == "admin":
                self._ensure_not_last_admin(user_id)
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._conn.commit()
        except Exception:
            if self._conn.in_transaction:
                self._conn.rollback()
            raise
        log.info("user deleted: %s", u["username"])

    def _ensure_not_last_admin(self, user_id: int) -> None:
        n = self._conn.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id!=?",
            (user_id,),
        ).fetchone()[0]
        if n == 0:
            raise AuthError("that would leave no active admin", 409)

    def set_password(
        self,
        user_id: int,
        new: str,
        *,
        must_change: bool = False,
        keep_session: str = "",
    ) -> None:
        check_password_policy(new)
        if not self.user(user_id):
            raise AuthError("unknown user", 404)
        self._conn.execute(
            "UPDATE users SET pw=?, must_change_pw=?, updated_ts=? WHERE id=?",
            (hash_password(new), int(must_change), int(time.time()), user_id),
        )
        if keep_session:
            self._conn.execute(
                "DELETE FROM sessions WHERE user_id=? AND id!=?",
                (user_id, keep_session),
            )
        else:
            self._conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        self._conn.commit()

    def change_own_password(
        self,
        user_id: int,
        current: str,
        new: str,
        keep_session: str,
        *,
        ip: str = "",
    ) -> None:
        subject = f"__pw__:{user_id}"
        rem = locked_for(ip, subject)
        if rem:
            raise AuthError(f"too many attempts — try again in {rem} s", 429)
        r = self._conn.execute(
            "SELECT pw FROM users WHERE id=?", (user_id,)
        ).fetchone()
        pw_ok = verify_password(current or "", r["pw"] if r else _dummy_hash())
        if not r or not pw_ok:
            _note_failure(ip, subject)
            time.sleep(FAIL_DELAY)
            raise AuthError("current password is wrong", 403)
        _clear_failures(ip, subject)
        if current == new:
            raise AuthError("new password must differ")
        self.set_password(user_id, new, keep_session=keep_session)

    # ---- login / sessions --------------------------------------------------

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        ip: str = "",
        ua: str = "",
        remember: bool = False,
    ) -> tuple[Principal, str, int | None]:
        """Timing- and oracle-resistant login. Returns ``(principal, raw_token, max_age)``."""
        username = (username or "").strip()
        rem = locked_for(ip, username)
        if rem:
            raise AuthError(f"too many attempts — try again in {rem} s", 429)

        r = self._conn.execute(
            "SELECT * FROM users WHERE username=?", (username,)
        ).fetchone()
        pw_ok = verify_password(password or "", r["pw"] if r else _dummy_hash())
        if not r or not pw_ok or r["disabled"]:
            _note_failure(ip, username)
            time.sleep(FAIL_DELAY)
            raise AuthError("invalid credentials", 401)

        _clear_failures(ip, username)
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        ttl = SESSION_TTL_REMEMBER if remember else SESSION_TTL
        sid = _tok_hash(token)
        self._conn.execute(
            "INSERT INTO sessions (id, user_id, created_ts, last_seen_ts, expires_ts,"
            " remember, ua, ip) VALUES (?,?,?,?,?,?,?,?)",
            (sid, r["id"], now, now, now + ttl, int(remember), ua[:200], ip[:64]),
        )
        self._conn.execute(
            "UPDATE users SET last_login_ts=? WHERE id=?", (now, r["id"])
        )
        self._conn.execute("DELETE FROM sessions WHERE expires_ts < ?", (now,))
        self._conn.commit()
        p = Principal(
            r["id"],
            r["username"],
            r["display"],
            r["role"],
            "cookie",
            sid,
            bool(r["must_change_pw"]),
        )
        return p, token, (ttl if remember else None)

    login = authenticate

    def principal_for_token(
        self, token: str, *, ip: str = ""
    ) -> Principal | None:
        if not token:
            return None
        sid = _tok_hash(token)
        now = int(time.time())
        r = self._conn.execute(
            "SELECT s.id AS sid, s.last_seen_ts, s.expires_ts, s.remember, u.*"
            " FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",
            (sid,),
        ).fetchone()
        if not r or r["expires_ts"] < now or r["disabled"]:
            return None
        if now - r["last_seen_ts"] > SESSION_TOUCH_EVERY:
            ttl = SESSION_TTL_REMEMBER if r["remember"] else SESSION_TTL
            self._conn.execute(
                "UPDATE sessions SET last_seen_ts=?, expires_ts=?, ip=? WHERE id=?",
                (now, now + ttl, ip[:64], sid),
            )
            self._conn.commit()
        return Principal(
            r["id"],
            r["username"],
            r["display"],
            r["role"],
            "cookie",
            sid,
            bool(r["must_change_pw"]),
        )

    def logout(self, session_id: str) -> None:
        self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        self._conn.commit()

    def revoke_session(
        self, session_id_prefix: str, user_id: int | None = None
    ) -> int:
        if len(session_id_prefix) < 8:
            raise AuthError("bad session id")
        sql = "DELETE FROM sessions WHERE id LIKE ? ESCAPE '\\'"
        args: list[Any] = [domain.escape_like(session_id_prefix) + "%"]
        if user_id is not None:
            sql += " AND user_id=?"
            args.append(user_id)
        cur = self._conn.execute(sql, args)
        self._conn.commit()
        return cur.rowcount

    def revoke_all(self, user_id: int, except_session: str = "") -> int:
        cur = self._conn.execute(
            "DELETE FROM sessions WHERE user_id=? AND id!=?",
            (user_id, except_session),
        )
        self._conn.commit()
        return cur.rowcount

    def sessions(self, user_id: int, current: str = "") -> list[dict[str, Any]]:
        now = int(time.time())
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE user_id=? AND expires_ts > ?"
            " ORDER BY last_seen_ts DESC",
            (user_id, now),
        ).fetchall()
        return [
            {
                "id": r["id"][:12],
                "created_ts": r["created_ts"],
                "last_seen_ts": r["last_seen_ts"],
                "expires_ts": r["expires_ts"],
                "remember": bool(r["remember"]),
                "ua": r["ua"],
                "ip": r["ip"],
                "current": r["id"] == current,
            }
            for r in rows
        ]

    # ---- audit ---------------------------------------------------------------

    def audit(
        self,
        user: Principal | None,
        action: str,
        target: str = "",
        detail: str = "",
        ip: str = "",
        ok: bool = True,
    ) -> None:
        cur = self._conn.execute(
            "INSERT INTO audit (ts, user, role, action, target, detail, ip, ok)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                user.username if user else "anonymous",
                user.role if user else "",
                action[:120],
                target[:200],
                detail[:600],
                ip[:64],
                int(ok),
            ),
        )
        if (cur.lastrowid or 0) % 200 == 0:
            self._conn.execute(
                "DELETE FROM audit WHERE id NOT IN"
                " (SELECT id FROM audit ORDER BY id DESC LIMIT ?)",
                (_AUDIT_KEEP,),
            )
        self._conn.commit()

    def audit_rows(
        self,
        *,
        user: str = "",
        action: str = "",
        q: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        args: list[Any] = []
        if user:
            where.append("user=?")
            args.append(user)
        if action:
            where.append("action LIKE ? ESCAPE '\\'")
            args.append(f"%{domain.escape_like(action)}%")
        if q:
            pat = f"%{domain.escape_like(q)}%"
            where.append(
                "(target LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\'"
                " OR action LIKE ? ESCAPE '\\')"
            )
            args.extend([pat, pat, pat])
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM audit{wsql}", args
        ).fetchone()[0]
        rows = [
            dict(r)
            for r in self._conn.execute(
                f"SELECT id, ts, user, role, action, target, detail, ip, ok,"
                f" datetime(ts,'unixepoch','localtime') AS at FROM audit{wsql}"
                f" ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            )
        ]
        users = [
            r[0]
            for r in self._conn.execute(
                "SELECT user FROM audit GROUP BY user ORDER BY COUNT(*) DESC LIMIT 30"
            )
        ]
        return {"total": total, "rows": rows, "users": users}


# --------------------------------------------------------------------------
# request-body summarizer for the audit trail
# --------------------------------------------------------------------------

def _redact_value(val: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return "…"
    if isinstance(val, dict):
        out: dict[str, Any] = {}
        for k, v in val.items():
            lk = str(k).lower()
            out[str(k)] = "•••" if any(s in lk for s in _REDACT) else _redact_value(v, depth + 1)
        return out
    if isinstance(val, list):
        return [_redact_value(item, depth + 1) for item in val[:25]]
    return val


def summarize_body(body: dict, limit: int = 600) -> str:
    if not isinstance(body, dict) or not body:
        return ""
    clean: dict[str, Any] = {}
    for k, v in body.items():
        lk = str(k).lower()
        if any(s in lk for s in _REDACT):
            clean[k] = "•••"
        elif isinstance(v, (list, dict)):
            redacted = _redact_value(v)
            try:
                s = json.dumps(redacted, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                s = str(redacted)
            clean[k] = s if len(s) <= 160 else s[:157] + "…"
        else:
            s = str(v)
            clean[k] = s if len(s) <= 160 else s[:157] + "…"
    try:
        out = json.dumps(clean, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        out = str(clean)
    return out if len(out) <= limit else out[: limit - 1] + "…"


def target_of(body: dict) -> str:
    if not isinstance(body, dict):
        return ""
    parts = []
    for k in (
        "artist", "album", "name", "username", "id", "album_id", "item_id",
        "command", "action", "script", "program", "kind", "query",
    ):
        v = body.get(k)
        if v not in (None, "", [], {}):
            s = str(v)
            parts.append(f"{k}={s if len(s) <= 60 else s[:57] + '…'}")
        if len(parts) >= 3:
            break
    return " ".join(parts)


def token_principal() -> Principal:
    return Principal(0, "token", "API token", "admin", "token", "")


@dataclass
class Perm:
    role: str = "operator"
    audit: bool = True
    fields: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_public(self) -> bool:
        return self.role == "public"


__all__ = [
    "Auth",
    "AuthError",
    "FAIL_DELAY",
    "LOCKOUT_AFTER",
    "LOCKOUT_SECONDS",
    "MIN_PASSWORD",
    "Perm",
    "Principal",
    "ROLES",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "SESSION_TTL_REMEMBER",
    "check_password_policy",
    "check_token_lockout",
    "check_username",
    "clear_cookie",
    "clear_token_failure",
    "hash_password",
    "locked_for",
    "parse_cookies",
    "record_token_failure",
    "session_cookie",
    "summarize_body",
    "target_of",
    "token_principal",
    "verify_password",
]

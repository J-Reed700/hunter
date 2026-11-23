"""Sign-in, sessions, own account, user management (admin) and the audit trail."""
from __future__ import annotations

from hunter import auth as authmod
from hunter.auth import AuthError
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log


def _fail(ctx: Ctx, exc: AuthError):
    return ctx.error(exc.code, str(exc))


def _raw_pw(ctx: Ctx, key: str = "password") -> str:
    val = ctx.body.get(key)
    if val is None:
        return ""
    if not isinstance(val, str):
        raise AuthError(f"{key} must be a string", 400)
    return val


# ---- session -----------------------------------------------------------------

@get("/api/auth/me", role="public")
def me(ctx: Ctx):
    with ctx.auth() as a:
        setup = a.setup_required
    return ctx.json(
        200,
        {
            "user": ctx.user.public() if ctx.user else None,
            "setup_required": setup,
            "roles": list(authmod.ROLES),
            "token_configured": bool(ctx.cfg.ui_token),
        },
    )


@post("/api/auth/setup", role="public", audit=False)
def setup(ctx: Ctx):
    """First run: create the admin atomically. Refused once any user exists."""
    with ctx.auth() as a:
        try:
            pw = _raw_pw(ctx, "password")
            u = a.create_first_admin(
                ctx.b("username"), pw, ctx.b("display")
            )
            p, token, max_age = a.authenticate(
                u["username"],
                pw,
                ip=ctx.ip,
                ua=ctx.header.get("User-Agent", ""),
                remember=True,
            )
        except AuthError as exc:
            return _fail(ctx, exc)
        a.audit(p, "auth.setup", u["username"], "admin account created", ctx.ip)
    log.info("setup complete: admin %s created from %s", u["username"], ctx.ip)
    return ctx.json(
        200,
        {"user": p.public()},
        {"Set-Cookie": authmod.session_cookie(token, max_age, secure=ctx.is_https)},
    )


@post("/api/auth/login", role="public", audit=False)
def login(ctx: Ctx):
    username = ctx.b("username")
    with ctx.auth() as a:
        if a.setup_required:
            return ctx.json(
                401, {"error": "setup required", "setup_required": True}
            )
        try:
            pw = _raw_pw(ctx, "password")
            p, token, max_age = a.authenticate(
                username,
                pw,
                ip=ctx.ip,
                ua=ctx.header.get("User-Agent", ""),
                remember=ctx.bb("remember"),
            )
        except AuthError as exc:
            a.audit(None, "auth.login", username[:64], str(exc), ctx.ip, ok=False)
            return _fail(ctx, exc)
        a.audit(
            p,
            "auth.login",
            p.username,
            "remember" if ctx.bb("remember") else "",
            ctx.ip,
        )
    return ctx.json(
        200,
        {"user": p.public()},
        {"Set-Cookie": authmod.session_cookie(token, max_age, secure=ctx.is_https)},
    )


@post("/api/auth/logout", role="viewer", audit=False)
def logout(ctx: Ctx):
    if ctx.user and ctx.user.session_id:
        with ctx.auth() as a:
            a.logout(ctx.user.session_id)
            a.audit(ctx.user, "auth.logout", "", "", ctx.ip)
    return ctx.json(
        200,
        {"ok": True},
        {"Set-Cookie": authmod.clear_cookie(secure=ctx.is_https)},
    )


@post("/api/auth/logout_all", role="viewer", audit=False)
def logout_all(ctx: Ctx):
    if not ctx.user or ctx.user.via != "cookie":
        return ctx.error(400, "cookie session required")
    with ctx.auth() as a:
        n = a.revoke_all(ctx.user.id, except_session=ctx.user.session_id)
        a.audit(ctx.user, "auth.logout_all", "", f"{n} sessions revoked", ctx.ip)
    return ctx.json(200, {"revoked": n})


@post("/api/auth/password", role="viewer", audit=False)
def password(ctx: Ctx):
    if not ctx.user or ctx.user.via != "cookie":
        return ctx.error(400, "cookie session required")
    with ctx.auth() as a:
        try:
            cur_pw = _raw_pw(ctx, "current")
            new_pw = _raw_pw(ctx, "new")
            a.change_own_password(
                ctx.user.id,
                cur_pw,
                new_pw,
                keep_session=ctx.user.session_id,
                ip=ctx.ip,
            )
        except AuthError as exc:
            a.audit(ctx.user, "auth.password", "", str(exc), ctx.ip, ok=False)
            return _fail(ctx, exc)
        a.audit(ctx.user, "auth.password", "", "changed", ctx.ip)
    return ctx.json(200, {"ok": True})


@get("/api/auth/sessions", role="viewer")
def sessions(ctx: Ctx):
    if not ctx.user or ctx.user.via != "cookie":
        return ctx.json(200, {"sessions": []})
    with ctx.auth() as a:
        return ctx.json(
            200, {"sessions": a.sessions(ctx.user.id, ctx.user.session_id)}
        )


@post("/api/auth/session/revoke", role="viewer", audit=False)
@post("/api/auth/sessions/revoke", role="viewer", audit=False)
def session_revoke(ctx: Ctx):
    if not ctx.user or ctx.user.via != "cookie":
        return ctx.error(400, "cookie session required")
    with ctx.auth() as a:
        try:
            n = a.revoke_session(ctx.b("id"), user_id=ctx.user.id)
        except AuthError as exc:
            return _fail(ctx, exc)
        a.audit(
            ctx.user,
            "auth.session.revoke",
            ctx.b("id")[:12],
            f"{n} revoked",
            ctx.ip,
        )
    return ctx.json(200, {"revoked": n})


# ---- user management (admin) ---------------------------------------------------

@get("/api/users", role="admin")
def users(ctx: Ctx):
    with ctx.auth() as a:
        return ctx.json(200, {"users": a.users(), "roles": list(authmod.ROLES)})


@post("/api/users/create", role="admin", audit=False)
def users_create(ctx: Ctx):
    with ctx.auth() as a:
        try:
            pw = _raw_pw(ctx, "password")
            u = a.create_user(
                ctx.b("username"),
                pw,
                ctx.b("role") or "viewer",
                ctx.b("display"),
                created_by=ctx.username,
                must_change_pw=ctx.bb("must_change_pw"),
            )
        except AuthError as exc:
            return _fail(ctx, exc)
        a.audit(ctx.user, "users.create", u["username"], f"role={u['role']}", ctx.ip)
    return ctx.json(200, {"user": u})


@post("/api/users/update", role="admin", audit=False)
def users_update(ctx: Ctx):
    uid = ctx.bi("id")
    role = ctx.b("role") or None
    display = ctx.body.get("display")
    disabled = ctx.body.get("disabled")
    if ctx.user and uid == ctx.user.id:
        if role and role != ctx.user.role:
            return ctx.error(409, "you cannot change your own role")
        if disabled:
            return ctx.error(409, "you cannot disable yourself")
    with ctx.auth() as a:
        try:
            u = a.update_user(
                uid,
                role=role,
                display=display if isinstance(display, str) else None,
                disabled=disabled if isinstance(disabled, bool) else None,
            )
        except AuthError as exc:
            return _fail(ctx, exc)
        a.audit(
            ctx.user,
            "users.update",
            u["username"],
            authmod.summarize_body(
                {k: v for k, v in ctx.body.items() if k != "id"}
            ),
            ctx.ip,
        )
    return ctx.json(200, {"user": u})


@post("/api/users/password", role="admin", audit=False)
def users_password(ctx: Ctx):
    uid = ctx.bi("id")
    with ctx.auth() as a:
        u = a.user(uid)
        if not u:
            return ctx.error(404, "unknown user")
        try:
            new_pw = _raw_pw(ctx, "new")
            keep = ctx.user.session_id if (ctx.user and uid == ctx.user.id) else ""
            a.set_password(
                uid,
                new_pw,
                must_change=not (ctx.user and uid == ctx.user.id),
                keep_session=keep,
            )
        except AuthError as exc:
            return _fail(ctx, exc)
        a.audit(ctx.user, "users.password", u["username"], "reset", ctx.ip)
    return ctx.json(200, {"ok": True})


@post("/api/users/delete", role="admin", audit=False)
def users_delete(ctx: Ctx):
    uid = ctx.bi("id")
    if ctx.user and uid == ctx.user.id:
        return ctx.error(409, "you cannot delete yourself")
    with ctx.auth() as a:
        u = a.user(uid)
        if not u:
            return ctx.error(404, "unknown user")
        try:
            a.delete_user(uid)
        except AuthError as exc:
            return _fail(ctx, exc)
        a.audit(ctx.user, "users.delete", u["username"], "", ctx.ip)
    return ctx.json(200, {"ok": True})


@get("/api/users/sessions", role="admin")
def users_sessions(ctx: Ctx):
    with ctx.auth() as a:
        return ctx.json(200, {"sessions": a.sessions(ctx.qi("id"))})


@post("/api/users/revoke", role="admin", audit=False)
def users_revoke(ctx: Ctx):
    uid = ctx.bi("id")
    with ctx.auth() as a:
        u = a.user(uid)
        if not u:
            return ctx.error(404, "unknown user")
        keep = ctx.user.session_id if (ctx.user and uid == ctx.user.id) else ""
        n = a.revoke_all(uid, except_session=keep)
        a.audit(ctx.user, "users.revoke", u["username"], f"{n} sessions", ctx.ip)
    return ctx.json(200, {"revoked": n})


# ---- audit trail ---------------------------------------------------------------

@get("/api/audit", role="admin")
def audit(ctx: Ctx):
    with ctx.auth() as a:
        return ctx.json(
            200,
            a.audit_rows(
                user=ctx.q("user"),
                action=ctx.q("action"),
                q=ctx.q("q"),
                limit=ctx.qi("limit", 100, 1, 500),
                offset=ctx.qi("offset", 0, 0),
            ),
        )

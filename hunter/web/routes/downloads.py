"""Downloads — take files out of the library."""
from __future__ import annotations

from hunter import downloads
from hunter.web.router import get, post
from hunter.web.server import Ctx


def _sel(ctx: Ctx) -> dict:
    return dict(
        item_ids=ctx.blist("item_ids", int, 20000),
        album_ids=ctx.blist("album_ids", int, 5000),
        artists=[a for a in ctx.blist("artists", str, 500) if a.strip()],
    )


def _may_touch(ctx: Ctx, b: dict) -> bool:
    return bool(ctx.user) and (
        ctx.user.role == "admin" or ctx.user.username == b.get("requested_by")
    )


def _public_bundle(b: dict) -> dict:
    return {k: v for k, v in b.items() if k != "path"}


@get("/api/dl/track", role="viewer")
def dl_track(ctx: Ctx):
    hit = downloads.track_file(ctx.cfg, ctx.qi("id"))
    if not hit:
        return ctx.error(404, "no such track on disk")
    path, name = hit
    return Ctx.stream_file(path, name, ctx.header.get("Range", ""))


@post("/api/downloads/estimate", role="viewer", audit=False)
def estimate(ctx: Ctx):
    sel = downloads.resolve(ctx.cfg, **_sel(ctx))
    max_bytes = getattr(
        ctx.cfg, "dl_max_bundle_bytes", downloads.MAX_BUNDLE_BYTES
    )
    return ctx.json(
        200,
        {
            "count": sel["count"],
            "bytes": sel["bytes"],
            "missing": sel["missing"],
            "max_bundle_bytes": max_bytes,
        },
    )


def _create_error_status(err: str) -> int:
    low = (err or "").lower()
    if any(k in low for k in ("too many active", "quota", "pending", "limit")):
        return 429
    if any(k in low for k in ("space", "disk", "cap is", "too large", "too many files")):
        return 413
    return 400


@post("/api/downloads", role="viewer")
def create(ctx: Ctx):
    res = downloads.create(
        ctx.cfg,
        title=ctx.b("title"),
        requested_by=ctx.username,
        role=ctx.user.role if ctx.user else "viewer",
        **_sel(ctx),
    )
    if res.get("ok"):
        out = dict(res)
        if isinstance(out.get("bundle"), dict):
            out["bundle"] = _public_bundle(out["bundle"])
        return ctx.json(200, out)
    return ctx.json(_create_error_status(res.get("error", "")), res)


@get("/api/downloads", role="viewer")
def index(ctx: Ctx):
    bundles = [
        _public_bundle(b)
        for b in downloads.list_bundles(ctx.cfg)
        if _may_touch(ctx, b)
    ]
    return ctx.json(
        200,
        {
            "bundles": bundles,
            "summary": downloads.summary(ctx.cfg),
        },
    )


@get("/api/downloads/file", role="viewer")
def file(ctx: Ctx):
    b = downloads.get(ctx.cfg, ctx.q("id"))
    if not b:
        return ctx.error(404, "unknown bundle")
    if not _may_touch(ctx, b):
        return ctx.error(403, "not your bundle")
    if b["status"] != "ready":
        return ctx.error(409, f"bundle is {b['status']}")
    info = downloads.bundle_file_info(ctx.cfg, b)
    if info is None:
        return ctx.error(410, "bundle file is gone (expired?)")
    p, name = info
    rng = (ctx.header.get("Range") or "").strip()
    is_head = getattr(ctx.handler, "command", "GET") == "HEAD"
    if not is_head and (not rng or rng.lower().startswith("bytes=0-")):
        downloads.mark_served(ctx.cfg, b["id"])
    return Ctx.stream_file(p, name, rng, ctype="application/zip")


@post("/api/downloads/cancel", role="viewer")
def cancel(ctx: Ctx):
    b = downloads.get(ctx.cfg, ctx.b("id"))
    if not b:
        return ctx.error(404, "unknown bundle")
    if not _may_touch(ctx, b):
        return ctx.error(403, "not your bundle")
    res = downloads.cancel(ctx.cfg, b["id"])
    return ctx.json(200 if res.get("ok") else 404, res)


@post("/api/downloads/delete", role="viewer")
def delete(ctx: Ctx):
    b = downloads.get(ctx.cfg, ctx.b("id"))
    if not b:
        return ctx.error(404, "unknown bundle")
    if not _may_touch(ctx, b):
        return ctx.error(403, "not your bundle")
    res = downloads.delete(ctx.cfg, b["id"])
    return ctx.json(200 if res.get("ok") else 404, res)

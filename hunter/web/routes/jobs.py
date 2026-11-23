"""Jobs: list, detail + live log tail, cancel, hunter CLI launcher."""
from __future__ import annotations

from hunter.jobs import hunter_argv
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log


@get("/api/jobs", role="operator")
def jobs(ctx: Ctx):
    j = common.jobs(ctx.cfg)
    j.refresh()
    rows = j.list(
        limit=ctx.qi("limit", 60, 1, 400),
        kind=ctx.q("kind"),
        status=ctx.q("status"),
        offset=ctx.qi("offset", 0, 0),
        refresh=False,
    )
    running = [r for r in rows if r["status"] == "running"]
    for r in running:
        t = j.tail(r["id"], lines=30, refresh=False)
        r["progress"] = t["progress"] if t else None
    return ctx.json(200, {"jobs": rows, "running": j.running_count(refresh=False)})


@get("/api/jobs/tail", role="operator")
def tail(ctx: Ctx):
    res = common.jobs(ctx.cfg).tail(
        ctx.q("id"), lines=ctx.qi("lines", 200, 10, 4000)
    )
    if res is None:
        return ctx.error(404, "unknown job")
    return ctx.json(200, res)


@post("/api/jobs/cancel")
def cancel(ctx: Ctx):
    res = common.jobs(ctx.cfg).cancel(ctx.b("id"), force=ctx.bb("force"))
    return ctx.json(200 if res["ok"] else 409, res)


@post("/api/jobs/prune_logs")
def prune_logs(ctx: Ctx):
    return ctx.json(200, {"pruned": common.jobs(ctx.cfg).prune_logs()})


# ---- hunter CLI launcher --------------------------------------------------

_HUNTER_CMDS = {
    "status": {
        "title": "hunter status",
        "blurb": "Counts + open wants, as the CLI prints them.",
    },
    "pass": {
        "title": "hunter pass",
        "blurb": "One full pass now (sync -> reconcile -> hunt -> mirror), tracked with a log.",
        "flags": [
            "--dry-run",
            "--apply",
            "--skip-spotify",
            "--skip-hunt",
            "--skip-playlists",
            "--skip-artists",
        ],
        "ints": {"--limit": (0, 50)},
    },
    "seed-gaps": {
        "title": "hunter seed-gaps",
        "blurb": "Seed every partial album in beets as a gap-fill want.",
        "flags": ["--dry-run"],
        "ints": {
            "--seed-limit": (0, 5000),
            "--min-missing": (1, 50),
            "--min-expected": (1, 100),
            "--max-expected": (2, 500),
        },
    },
    "seed-upgrades": {
        "title": "hunter seed-upgrades",
        "blurb": "Seed lossy-only albums as lowest-priority upgrade wants.",
        "flags": ["--dry-run"],
        "ints": {"--seed-limit": (0, 5000), "--min-tracks": (1, 50)},
    },
}


@get("/api/hunter/commands", role="operator")
def hunter_commands(ctx: Ctx):
    return ctx.json(
        200, {"commands": [{"name": k, **v} for k, v in _HUNTER_CMDS.items()]}
    )


@post("/api/hunter/run")
def hunter_run(ctx: Ctx):
    name = ctx.b("command")
    spec = _HUNTER_CMDS.get(name)
    if not spec:
        return ctx.error(400, "unknown hunter command")
    raw_flags = ctx.body.get("flags")
    if raw_flags is not None and not isinstance(raw_flags, list):
        return ctx.error(400, "flags must be a list")
    raw_ints = ctx.body.get("ints")
    if raw_ints is not None and not isinstance(raw_ints, dict):
        return ctx.error(400, "ints must be an object")
    argv = [name]
    for f in raw_flags or ():
        if f in spec.get("flags", []):
            argv.append(f)
    for k, v in (raw_ints or {}).items():
        lim = spec.get("ints", {}).get(k)
        if lim is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            return ctx.error(400, f"{k} must be an integer")
        try:
            iv = int(v)
        except ValueError:
            return ctx.error(400, f"{k} must be an integer")
        if lim[0] <= iv <= lim[1] and iv:
            argv += [k, str(iv)]
    if ctx.bb("verbose"):
        argv.append("-v")
    res = common.jobs(ctx.cfg).start_local(
        kind="hunter",
        title=" ".join(["hunter", *argv]),
        argv=hunter_argv(*argv),
        mode="dry-run" if "--dry-run" in argv else "run",
        exclusive="hunter-pass" if name == "pass" else None,
    )
    return ctx.json(200 if res["ok"] else 409, res)

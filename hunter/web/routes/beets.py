"""The beets console: pipeline, curated scripts, and `beet` command desk."""
from __future__ import annotations

import os
import shlex

from hunter import beetcmd, beetsops
from hunter.dockerctl import DockerClient, DockerError
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx


def _docker(ctx: Ctx) -> DockerClient:
    return DockerClient(ctx.cfg.docker_sock)


# ---- pipeline ---------------------------------------------------------------

@get("/api/beets/pipeline", role="operator")
def pipeline(ctx: Ctx):
    cfg = ctx.cfg
    d = _docker(ctx)
    out = {
        "console": d.available,
        "watcher": beetsops.watcher_status(cfg, d) if d.available else None,
        "sources": beetsops.source_dirs(cfg, d)["sources"] if d.available else [],
        "maintenance": beetsops.maintenance_status(cfg),
        "quarantine": beetsops.quarantine_tree(cfg),
        "schedule": beetsops.load_schedule(cfg),
        "reports": beetsops.maintenance_reports(cfg),
        "jobs": common.jobs(cfg).list(limit=8),
    }
    return ctx.json(200, out)


@get("/api/beets/watcher", role="operator")
def watcher(ctx: Ctx):
    d = _docker(ctx)
    if not d.available:
        return ctx.error(503, "docker socket not mounted")
    return ctx.json(200, beetsops.watcher_status(ctx.cfg, d, ttl=5))


@post("/api/beets/watcher/restart")
def watcher_restart(ctx: Ctx):
    d = _docker(ctx)
    if not d.available:
        return ctx.error(503, "docker socket not mounted")
    res = beetsops.restart_watcher(ctx.cfg, d)
    if res.get("ok"):
        with common.state(ctx.cfg) as st:
            st.log_event("automation", detail="beets watcher bounced via ui")
    return ctx.json(200 if res["ok"] else 502, res)


@get("/api/beets/config", role="operator")
def config(ctx: Ctx):
    d = _docker(ctx)
    if not d.available:
        return ctx.error(503, "docker socket not mounted")
    return ctx.json(200, beetsops.watcher_config(ctx.cfg, d))


@get("/api/beets/sources", role="operator")
def sources(ctx: Ctx):
    d = _docker(ctx)
    if not d.available:
        return ctx.error(503, "docker socket not mounted")
    per_source = ctx.qi("n", 60, 1, 500)
    beetsops.cache.drop(f"source_dirs:{int(per_source)}")
    return ctx.json(200, beetsops.source_dirs(ctx.cfg, d, ttl=0, per_source=per_source))


@get("/api/beets/source_dir", role="operator")
def source_dir(ctx: Ctx):
    d = _docker(ctx)
    res = beetsops.source_dir_files(ctx.cfg, d, ctx.q("source"), ctx.q("dir"))
    if res is None:
        return ctx.error(404, "unknown source/dir")
    return ctx.json(200, res)


@post("/api/beets/import")
def import_dir(ctx: Ctx):
    d = _docker(ctx)
    if not d.available:
        return ctx.error(503, "docker socket not mounted")
    res = beetsops.import_dir(
        ctx.cfg, d, common.jobs(ctx.cfg), ctx.b("source"), ctx.b("dir")
    )
    return ctx.json(200 if res["ok"] else 409, res)


@post("/api/beets/smart_merge")
def smart_merge(ctx: Ctx):
    d = _docker(ctx)
    src = beetsops.resolve_source_dir(ctx.cfg, d, ctx.b("source"), ctx.b("dir"))
    if not src:
        return ctx.error(404, "unknown source/dir")
    res = beetsops.smart_merge(common.jobs(ctx.cfg), src[1], ctx.bb("apply"))
    return ctx.json(200 if res["ok"] else 409, res)


@post("/api/beets/rescan")
def rescan(ctx: Ctx):
    d = _docker(ctx)
    if not d.available:
        return ctx.error(503, "docker socket not mounted")
    res = beetsops.trigger_rescan(ctx.cfg, d, ctx.b("app") or "all")
    return ctx.json(200 if res["ok"] else 502, res)


@post("/api/beets/maintenance")
def maintenance(ctx: Ctx):
    res = beetsops.start_script(common.jobs(ctx.cfg), "maintenance")
    beetsops.cache.drop("maintenance_status")
    return ctx.json(200 if res["ok"] else 409, res)


# ---- quarantine ------------------------------------------------------------

@get("/api/beets/quarantine", role="operator")
def quarantine(ctx: Ctx):
    if ctx.q("refresh") == "1":
        beetsops.cache.drop("quarantine_tree")
    return ctx.json(200, beetsops.quarantine_tree(ctx.cfg))


@get("/api/beets/quarantine/entries", role="operator")
def quarantine_entries(ctx: Ctx):
    res = beetsops.quarantine_entries(
        ctx.cfg,
        ctx.q("bucket"),
        ctx.q("reason"),
        ctx.qi("offset", 0, 0),
        ctx.qi("limit", 100, 1, 500),
        ctx.q("q"),
    )
    if res is None:
        return ctx.error(404, "unknown bucket/reason")
    return ctx.json(200, res)


@get("/api/beets/quarantine/entry", role="operator")
def quarantine_entry(ctx: Ctx):
    res = beetsops.quarantine_entry(
        ctx.cfg, ctx.q("bucket"), ctx.q("reason"), ctx.q("name")
    )
    if res is None:
        return ctx.error(404, "unknown entry")
    return ctx.json(200, res)


# ---- logs ----------------------------------------------------------------

@get("/api/beets/logs", role="operator")
def logs(ctx: Ctx):
    name = ctx.q("name")
    if not name:
        return ctx.json(200, {"sources": list(beetsops.log_sources(ctx.cfg))})
    text = beetsops.read_log(
        ctx.cfg, _docker(ctx), name, ctx.qi("lines", 200, 10, 2000)
    )
    if text is None:
        return ctx.error(404, "unknown log")
    return ctx.json(200, {"name": name, "text": text})


@get("/api/beets/report", role="operator")
def report(ctx: Ctx):
    text = beetsops.read_report(
        ctx.cfg, ctx.q("name"), ctx.qi("lines", 400, 10, 5000)
    )
    if text is None:
        return ctx.error(404, "unknown report")
    return ctx.json(200, {"name": ctx.q("name"), "text": text})


# ---- scripts --------------------------------------------------------------

@get("/api/beets/scripts", role="operator")
def scripts(ctx: Ctx):
    return ctx.json(200, beetsops.script_catalogue(common.jobs(ctx.cfg), ctx.cfg))


@post("/api/beets/scripts/run")
def scripts_run(ctx: Ctx):
    if not ctx.cfg.beets_console_available:
        return ctx.error(503, "docker socket not mounted")
    extra = ctx.body.get("args")
    res = beetsops.start_script(
        common.jobs(ctx.cfg),
        ctx.b("id"),
        apply=ctx.bb("apply"),
        date=ctx.b("date") or None,
        limit=ctx.bi("limit") or None,
        extra=extra if isinstance(extra, list) else None,
    )
    return ctx.json(200 if res["ok"] else 409, res)


# ---- the beet command desk --------------------------------------------------

@get("/api/beets/commands", role="operator")
def commands(ctx: Ctx):
    return ctx.json(
        200,
        {
            "commands": beetcmd.catalogue(),
            "soft_max": beetcmd.SOFT_MAX,
            "wrapper": " ".join(beetcmd.beet_argv(ctx.cfg)),
        },
    )


def _allowed_roots(ctx: Ctx) -> tuple[str, ...] | None:
    roots = [
        getattr(ctx.cfg, "beets_music_root", "") or "",
        getattr(ctx.cfg, "beets_quarantine_root", "") or "",
    ]
    cleaned = tuple(r.strip() for r in roots if isinstance(r, str) and r.strip())
    return cleaned or None


def _build(ctx: Ctx, apply: bool, for_preview: bool = False) -> list[str]:
    roots = _allowed_roots(ctx)
    raw = ctx.b("raw")
    if raw:
        return beetcmd.build_raw(
            raw, apply=apply, for_preview=for_preview, allowed_roots=roots
        )
    opts = ctx.body.get("options") if isinstance(ctx.body.get("options"), dict) else {}
    pos = ctx.body.get("positional") if isinstance(ctx.body.get("positional"), list) else []
    return beetcmd.build(
        ctx.b("command"),
        query=ctx.b("query"),
        options=opts,
        positional=pos,
        apply=apply,
        for_preview=for_preview,
        allowed_roots=roots,
    )


def _quick(ctx: Ctx, argv: list[str], timeout: int = 45) -> dict:
    d = _docker(ctx)
    res = d.exec_run(
        ctx.cfg.beets_container,
        beetcmd.beet_argv(ctx.cfg) + argv,
        user=ctx.cfg.beets_exec_user_str,
        timeout=timeout,
    )
    out = res["output"]
    if len(out) > 200_000:
        out = out[:200_000] + "\n… (truncated)"
    return {"exit_code": res["exit_code"], "output": out}


@post("/api/beets/preview")
def preview(ctx: Ctx):
    """Blast radius for a command: what `beet ls` matches its query."""
    if not ctx.cfg.beets_console_available:
        return ctx.error(503, "docker socket not mounted")
    try:
        argv = _build(ctx, apply=False, for_preview=True)
    except (beetcmd.PolicyError, ValueError) as exc:
        return ctx.error(400, str(exc))
    cmd, qtoks, album = beetcmd.query_of(argv)
    if cmd is None:
        return ctx.error(400, "unknown command")
    out = {
        "argv": argv,
        "command": shlex.join(argv),
        "cls": cmd.cls,
        "query": qtoks,
        "album": album,
        "count": None,
        "sample": [],
        "needs_confirm": False,
    }
    if cmd.query == "none":
        return ctx.json(200, out)
    try:
        count, sample, rc = beetsops.count_matches(ctx.cfg, _docker(ctx), qtoks, album)
    except DockerError as exc:
        return ctx.error(502, str(exc))
    out["count"] = count
    out["sample"] = sample
    out["exit_code"] = rc
    out["needs_confirm"] = beetcmd.needs_confirm(cmd, qtoks, count, argv)
    out["too_many"] = count > beetcmd.HARD_MAX
    if rc not in (0, None):
        out["error"] = f"beet list exited {rc}"
    return ctx.json(200, out)


@post("/api/beets/run")
def run(ctx: Ctx):
    """Run a beet command as a job (or attached when quick=true for read cmds)."""
    cfg = ctx.cfg
    if not cfg.beets_console_available:
        return ctx.error(503, "docker socket not mounted")
    apply = ctx.bb("apply")
    try:
        argv = _build(ctx, apply=apply)
    except (beetcmd.PolicyError, ValueError) as exc:
        return ctx.error(400, str(exc))
    cmd, qtoks, album = beetcmd.query_of(argv)
    if cmd is None:
        return ctx.error(400, "unknown command")
    if cmd.cls == "mutate" and apply:
        err = beetsops.guard_mutation(
            cfg,
            _docker(ctx),
            argv,
            allow_all=ctx.bb("allow_all"),
            confirm_count=ctx.bi("confirm_count", -1),
        )
        if err is not None:
            return ctx.json(err[0], err[1])
    if ctx.bb("quick") and cmd.quick:
        try:
            res = _quick(ctx, argv)
        except DockerError as exc:
            return ctx.error(502, str(exc))
        return ctx.json(200, {"quick": True, "argv": argv, **res})
    mode = "apply" if apply else ("dry-run" if cmd.cls == "mutate" else "run")
    title = f"beet {shlex.join(argv)}"
    res = common.jobs(cfg).start_beets(
        kind="beet",
        title=title[:120],
        argv=beetcmd.beet_argv(cfg) + argv,
        mode=mode,
        meta={"cmd": cmd.name, "query": " ".join(qtoks), "cls": cmd.cls},
        exclusive=f"beet:{cmd.name}:{' '.join(qtoks)}" if cmd.cls == "mutate" else None,
    )
    if res["ok"] and cmd.cls == "mutate" and apply:
        common.invalidate_library()
        with common.state(cfg) as st:
            st.log_event(
                "automation", detail=f"beet {shlex.join(argv)} started via console"
            )
    return ctx.json(200 if res["ok"] else 409, res)


@get("/api/beets/db_stats", role="operator")
def db_stats(ctx: Ctx):
    cfg = ctx.cfg
    out: dict = {}
    for suffix, key in (("", "db"), ("-wal", "wal"), ("-shm", "shm")):
        p = str(cfg.beets_db) + suffix
        try:
            out[key] = os.path.getsize(p)
        except OSError:
            out[key] = None
    try:
        out["import_log"] = os.path.getsize(cfg.beets_db.parent / "import.log")
        out["import_log_mtime"] = os.path.getmtime(cfg.beets_db.parent / "import.log")
    except OSError:
        pass
    return ctx.json(200, out)

"""Automation programs, tunables, peers (ban/unban/reputation), backups."""
from __future__ import annotations

import secrets
import sqlite3
import threading
import time

from hunter import config as config_mod, db
from hunter.config import HunterConfig
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log
_BACKUP_LOCK = threading.Lock()
_LAST_BACKUP_MONO = 0.0
_BACKUP_COOLDOWN_S = 15.0


@get("/api/automation", role="operator")
def automation(ctx: Ctx):
    cfg = ctx.cfg
    with common.state(cfg) as st:
        sc = st.source_counts()
        followed = len(st.followed_artists())
        pl_hunting = sum(1 for p in st.playlists() if p["hunt"])
        last_reseed = st.get_meta("last_reseed", "0")
    upgrade_candidates = gap_candidates = None
    if "upgrade" not in sc or ctx.q("count") == "1":
        try:
            from hunter import pipeline
            upgrade_candidates = len(pipeline.lossy_rows(cfg))
        except Exception:                                    # noqa: BLE001
            upgrade_candidates = None
    if "gapfill" not in sc or ctx.q("count") == "1":
        try:
            from hunter import pipeline
            gap_candidates = len(pipeline.gap_rows(cfg))
        except Exception:                                    # noqa: BLE001
            gap_candidates = None
    try:
        last_reseed_f = float(last_reseed)
    except ValueError:
        last_reseed_f = 0.0
    return ctx.json(
        200,
        {
            "programs": {
                "spotify_sync": {
                    "connected": cfg.spotify_configured,
                    "want_sources": list(cfg.want_sources),
                },
                "prowlarr": {
                    "configured": cfg.prowlarr_configured,
                    "enabled": cfg.prowlarr_fallback,
                    "after_misses": cfg.prowlarr_after_misses,
                },
                "gapfill": sc.get("gapfill", {}),
                "gapfill_candidates": gap_candidates,
                "upgrade": sc.get("upgrade", {}),
                "upgrade_candidates": upgrade_candidates,
                "release_watch": {
                    "followed": followed,
                    "check_hours": cfg.artist_check_hours,
                },
                "playlist_hunts": pl_hunting,
                "sources": sc,
                "last_reseed_age_days": (
                    round((time.time() - last_reseed_f) / 86_400, 1)
                    if last_reseed_f
                    else None
                ),
            },
            "settings": {
                "prowlarr_after_misses": cfg.prowlarr_after_misses,
                "prowlarr_fallback": cfg.prowlarr_fallback,
                "interval": cfg.interval,
                "max_grabs_per_pass": cfg.max_grabs_per_pass,
                "min_bitrate": cfg.min_bitrate,
                "max_attempts": cfg.max_attempts,
                "search_miss_cap": cfg.search_miss_cap,
                "prefer_lossless": cfg.prefer_lossless,
            },
            "readonly": {
                "owned_coverage": cfg.owned_coverage,
                "search_timeout": cfg.search_timeout,
                "pass_deadline": cfg.pass_deadline,
                "grab_stale_seconds": cfg.grab_stale_seconds,
                "peer_ban_strikes": cfg.peer_ban_strikes,
                "min_candidate_ratio": cfg.min_candidate_ratio,
                "max_candidate_ratio": cfg.max_candidate_ratio,
                "playlist_prefix": cfg.playlist_prefix,
                "playlist_exclude": cfg.playlist_exclude,
            },
            "limits": {
                k: v for k, v in HunterConfig.UI_TUNABLE.items() if v is not None
            },
            "apply": cfg.apply,
        },
    )


@post("/api/automation/settings")
def settings(ctx: Ctx):
    updates = ctx.body if isinstance(ctx.body, dict) else {}
    applied = config_mod.save_tunables(ctx.cfg, updates)
    if not applied:
        return ctx.error(400, "no valid settings in body")
    log.info("automation settings updated via ui: %s", applied)
    return ctx.json(200, {"applied": applied})


@post("/api/automation/program")
def program(ctx: Ctx):
    cfg = ctx.cfg
    prog, action = ctx.b("program"), ctx.b("action")
    if prog not in ("gapfill", "upgrade") or action not in (
        "seed",
        "pause",
        "resume",
        "clear",
    ):
        return ctx.error(
            400, "program (gapfill|upgrade) + action (seed|pause|resume|clear)"
        )
    with common.state(cfg) as st:
        if action == "pause":
            n = st.set_source_status(prog, "wanted", "skipped")
            st.log_event("automation", detail=f"{prog} paused ({n} wants parked)")
            return ctx.json(200, {"paused": n})
        if action == "resume":
            n = st.set_source_status(prog, "skipped", "wanted")
            st.log_event("automation", detail=f"{prog} resumed ({n} wants back)")
            return ctx.json(200, {"resumed": n})
        if action == "clear":
            n = st.drop_wants("%", "%", prog, literal=True)
            st.log_event(
                "automation", detail=f"{prog} CLEARED via ui ({n} wants skipped)"
            )
            return ctx.json(200, {"cleared": n})

        from hunter import pipeline

        limit = max(0, ctx.bi("limit", 0))
        if prog == "gapfill":
            new = pipeline.seed_gaps(cfg, st, limit=limit, apply=True)
        else:
            new = pipeline.seed_upgrades(cfg, st, limit=limit, apply=True)
        st.set_meta("last_reseed", str(time.time()))
        st.log_event("automation", detail=f"{prog} seeded: {new} new wants via ui")
    return ctx.json(200, {"seeded": new})


# ---- peers -----------------------------------------------------------------

@get("/api/peers", role="operator")
def peers(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        banned = [dict(r) for r in st.banned_peer_rows()]
        stats = [dict(r) for r in st.peer_stat_rows()]
        rep = st.peer_reputation()
    bset = {b["username"] for b in banned}
    for s in stats:
        s["reputation"] = round(rep.get(s["username"], 0.0), 3)
        s["banned"] = s["username"] in bset
    stats.sort(key=lambda s: (-s["grabs"], s["username"]))
    return ctx.json(
        200,
        {"banned": banned, "stats": stats[:200], "strikes": ctx.cfg.peer_ban_strikes},
    )


@post("/api/ban")
def ban(ctx: Ctx):
    username = ctx.b("username")
    if not username:
        return ctx.error(400, "username required")
    with common.state(ctx.cfg) as st:
        added = st.ban_peer(username, ctx.b("reason") or "via ui")
    return ctx.json(200, {"banned": added})


@post("/api/unban")
def unban(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        removed = st.unban_peer(ctx.b("username"))
    return ctx.json(200, {"removed": removed})


# ---- backups ---------------------------------------------------------------

@get("/api/backups", role="operator")
def backups(ctx: Ctx):
    cfg = ctx.cfg
    out: dict = {"hunter": [], "beets": []}
    for key, d in (
        ("hunter", cfg.state_dir / "backups"),
        ("beets", cfg.beets_automation_dir / "backups"),
    ):
        try:
            rows = []
            for p in d.iterdir():
                if p.is_file():
                    st = p.stat()
                    rows.append(
                        {"name": p.name, "size": st.st_size, "mtime": st.st_mtime}
                    )
            rows.sort(key=lambda r: r["mtime"], reverse=True)
            out[key] = rows[:30]
        except OSError:
            out[key] = []
    try:
        out["state_db_size"] = cfg.state_db.stat().st_size
        out["beets_db_size"] = cfg.beets_db.stat().st_size
    except OSError:
        pass
    return ctx.json(200, out)


@post("/api/backups/hunter")
def backup_hunter(ctx: Ctx):
    global _LAST_BACKUP_MONO
    cfg = ctx.cfg
    d = cfg.state_dir / "backups"
    target = d / f"hunter.db.{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}.manual"
    with _BACKUP_LOCK:
        now = time.monotonic()
        if _LAST_BACKUP_MONO and (now - _LAST_BACKUP_MONO) < _BACKUP_COOLDOWN_S:
            rem = int(_BACKUP_COOLDOWN_S - (now - _LAST_BACKUP_MONO)) + 1
            return ctx.error(429, f"backup recently created — wait {rem}s")
        try:
            d.mkdir(parents=True, exist_ok=True)
            db.backup_sqlite(cfg.state_db, target)
            db.prune_backups(d, keep_days=14)
        except (sqlite3.Error, OSError) as exc:
            log.warning("manual backup failed: %s", exc)
            return ctx.error(500, f"backup failed: {exc}")
        _LAST_BACKUP_MONO = time.monotonic()
    return ctx.json(200, {"backup": target.name})

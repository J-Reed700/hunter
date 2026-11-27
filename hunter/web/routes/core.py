"""Status, pulse, transfers, pass history, events, art proxy, apply switch."""
from __future__ import annotations

import json
import re
import time

import requests

from hunter import config as config_mod, domain, http_client, libquery, storage
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_ART_MAX_BYTES = 5 * 1024 * 1024
_ART_ALLOWED_TYPES = frozenset({"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif"})


@get("/api/status")
def status(ctx: Ctx):
    from hunter import __version__

    cfg = ctx.cfg
    is_op = bool(ctx.user and ctx.user.can("operator"))
    with common.state(cfg) as st:
        counts = st.counts()
    heartbeat = None
    raw: dict = {}
    hb_file = cfg.state_dir / "heartbeat.json"
    if hb_file.exists():
        try:
            raw = json.loads(hb_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            heartbeat = {"ts": raw.get("ts"), "status": raw.get("status")}
        else:
            raw = {}
    sync = raw.get("sync") if isinstance(raw.get("sync"), dict) else {}
    sync_err = sync.get("error", "") or ""
    low = sync_err.lower()
    spotify = {
        "configured": cfg.spotify_configured,
        "ok": bool(cfg.spotify_configured) and not sync_err,
        "error": sync_err if is_op else ("sync error" if sync_err else ""),
        "needs_spdc": ("premium" in low or "403" in low or "subscription" in low),
    }
    jobs = common.jobs(cfg)
    running = jobs.running()
    return ctx.json(
        200,
        {
            "apply": cfg.apply,
            "counts": counts,
            "heartbeat": heartbeat,
            "spotify": spotify,
            "interval": cfg.interval,
            "version": __version__,
            "jobs_running": len(running),
            "jobs": [
                {
                    "id": j["id"],
                    "title": j["title"],
                    "kind": j["kind"],
                    "started_ts": j["started_ts"],
                }
                for j in running[:8]
            ] if is_op else [],
            "console": cfg.beets_console_available if is_op else False,
            "storage": storage.summary(cfg),
            "slskd": cfg.slskd_configured,
            "prowlarr": cfg.prowlarr_configured,
            "navidrome": bool(cfg.navidrome_user),
            "audiomuse": bool(getattr(cfg, "audiomuse_token", "")),
            "music_root": str(cfg.music_root) if is_op else "",
            "quarantine_dir": (
                str(cfg.quarantine_dir)
                if is_op and getattr(cfg, "quarantine_enabled", True)
                else None
            ),
        },
    )


@get("/api/pulse")
def pulse(ctx: Ctx):
    """Everything the dashboard needs in one read."""
    cfg = ctx.cfg
    is_op = bool(ctx.user and ctx.user.can("operator"))
    with common.state(cfg) as st:
        counts = st.counts()
        sc = st.source_counts()
        passes = st.recent_passes_parsed(12)
        events = [dict(r) for r in st.events(limit=12)]
        recent_done = [
            dict(r)
            for r in st.wants_query(
                statuses=["done"], source="", text="", sort="updated", limit=8
            )
        ]

    try:
        lib = libquery.pulse_counts(cfg)
    except Exception:                                 # noqa: BLE001
        lib = None

    jobs_out = common.jobs(cfg).list(limit=6) if is_op else []
    return ctx.json(
        200,
        {
            "counts": counts,
            "sources": sc,
            "passes": passes,
            "events": events,
            "recent_done": recent_done,
            "library": lib,
            "jobs": jobs_out,
            "apply": cfg.apply,
            "interval": cfg.interval,
        },
    )


@get("/api/transfers")
def transfers(ctx: Ctx):
    cfg = ctx.cfg
    with common.state(cfg) as st:
        grabbed = [
            dict(r)
            for r in st.wants_query(
                statuses=["grabbed"], source="", text="", sort="updated", limit=200, offset=0
            )
        ]
    queue_active = 0
    slskd_ok = False
    raw_dl: list = []
    if cfg.slskd_configured:
        from hunter.slskd_client import SlskdClient, SlskdError

        try:
            with SlskdClient(cfg.slskd_url, cfg.slskd_api_key) as client:
                dl = client.download_states()
                slskd_ok = True
                queue_active = sum(s["active"] for s in dl.values())
                for w in grabbed:
                    norm_dir = (w.get("grabbed_dir") or "").replace("\\", "/")
                    w["dl"] = dl.get(
                        (w.get("grabbed_username") or "", norm_dir)
                    ) or dl.get((w.get("grabbed_username") or "", w.get("grabbed_dir") or ""))
                if ctx.q("raw") == "1":
                    for user in client.downloads()[:60]:
                        for d in user.get("directories") or []:
                            files = d.get("files") or []
                            raw_dl.append(
                                {
                                    "username": user.get("username"),
                                    "dir": d.get("directory"),
                                    "files": len(files),
                                    "done": sum(
                                        1
                                        for f in files
                                        if "Succeeded" in (f.get("state") or "")
                                    ),
                                    "errored": sum(
                                        1
                                        for f in files
                                        if "Completed" in (f.get("state") or "")
                                        and "Succeeded" not in (f.get("state") or "")
                                    ),
                                    "bytes": sum(int(f.get("size") or 0) for f in files),
                                    "transferred": sum(
                                        int(f.get("bytesTransferred") or 0) for f in files
                                    ),
                                    "speed": sum(
                                        float(f.get("averageSpeed") or 0)
                                        for f in files
                                        if "InProgress" in (f.get("state") or "")
                                    ),
                                }
                            )
        except SlskdError as exc:
            common.log.warning("transfers fetch failed: %s", exc)
    return ctx.json(
        200,
        {
            "grabbed": grabbed,
            "queue_active": queue_active,
            "slskd_ok": slskd_ok,
            "raw": raw_dl,
        },
    )


@get("/api/pass_history")
def pass_history(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        rows = st.recent_passes_parsed(ctx.qi("limit", 40, 1, 200))
    return ctx.json(200, {"passes": rows})


@get("/api/events")
def events(ctx: Ctx):
    kind = ctx.q("kind")
    limit = ctx.qi("limit", 100, 1, 500)
    offset = ctx.qi("offset", 0, 0)
    with common.state(ctx.cfg) as st:
        rows = st.events(limit=limit, offset=offset, kind=kind)
        kinds = st.event_kinds()
    return ctx.json(200, {"events": [dict(r) for r in rows], "kinds": kinds})


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


@get("/api/art/", prefix=True)
def art(ctx: Ctx):
    cfg = ctx.cfg
    rgid = ctx.path.rsplit("/", 1)[1].lower()
    if not _UUID_RE.fullmatch(rgid):
        return ctx.error(400, "bad rgid")
    art_dir = cfg.state_dir / "art"
    art_dir.mkdir(parents=True, exist_ok=True)
    cached = art_dir / f"{rgid}.jpg"
    miss = art_dir / f"{rgid}.miss"
    if not cached.exists():
        try:
            if time.time() - miss.stat().st_mtime < 604_800:
                return ctx.error(404, "no art")
        except OSError:
            pass
    if not cached.exists():
        got = False
        sess = common.ART_CLIENT.session(cfg)
        for size in ("250", "500"):
            try:
                resp = sess.get(
                    f"https://coverartarchive.org/release-group/{rgid}/front-{size}",
                    timeout=10.0,
                    stream=True,
                    raise_for_status=False,
                )
            except (http_client.HttpError, requests.RequestException):
                continue
            try:
                if resp.status_code == 404:
                    break
                if resp.status_code != 200:
                    continue
                ct = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ct not in _ART_ALLOWED_TYPES:
                    continue
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > _ART_MAX_BYTES:
                    continue
                buf = bytearray()
                too_big = False
                for chunk in resp.iter_content(65536):
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > _ART_MAX_BYTES:
                        too_big = True
                        break
                if too_big or not buf:
                    continue
                raw_bytes = bytes(buf)
                if _detect_image_mime(raw_bytes) is None:
                    continue
                domain.atomic_write_bytes(cached, raw_bytes)
                got = True
                break
            finally:
                resp.close()
        if not got:
            try:
                miss.touch()
            except OSError:
                pass
            return ctx.error(404, "no art")
    try:
        cached_bytes = cached.read_bytes()
    except OSError:
        return ctx.error(404, "no art")
    return ctx.send(
        200,
        cached_bytes,
        _detect_image_mime(cached_bytes) or "image/jpeg",
        {"Cache-Control": "private, max-age=86400"},
    )


@post("/api/pass")
def run_pass(ctx: Ctx):
    """Spawn a pass now. With flags -> tracked as a job with a log."""
    flags = []
    for f in (
        "skip-spotify",
        "skip-hunt",
        "skip-playlists",
        "skip-artists",
        "dry-run",
        "apply",
    ):
        if ctx.bb(f.replace("-", "_")) or ctx.bb(f):
            flags.append(f"--{f}")
    if "--dry-run" in flags and "--apply" in flags:
        return ctx.error(400, "cannot combine dry_run and apply")
    limit = ctx.bi("limit")
    if limit:
        flags += ["--limit", str(max(1, min(limit, 50)))]
    if ctx.bb("track") or flags:
        from hunter.jobs import hunter_argv

        res = common.jobs(ctx.cfg).start_local(
            kind="hunter",
            title="hunter pass " + " ".join(flags),
            argv=hunter_argv("pass", *flags),
            mode="run",
            exclusive="hunter-pass",
        )
        if not res["ok"]:
            return ctx.json(409, {"error": res["error"], "detail": res["error"]})
        return ctx.json(200, {"detail": "pass started", "job": res["job"]})
    common.spawn_pass()
    return ctx.json(200, {"detail": "pass started (no-op if one is running)"})


@post("/api/apply")
def set_apply(ctx: Ctx):
    if "apply" not in ctx.body:
        return ctx.error(400, "apply boolean required")
    target = ctx.bb("apply")
    config_mod.save_apply_override(ctx.cfg, target)
    common.log.info("apply mode set to %s via UI", target)
    return ctx.json(200, {"apply": target})


@get("/api/services", role="operator")
def services(ctx: Ctx):
    """Container health of the pipeline neighbours (docker socket)."""
    from hunter.dockerctl import DockerClient

    cfg = ctx.cfg
    d = DockerClient(cfg.docker_sock)
    if not d.available:
        return ctx.json(200, {"available": False, "services": []})

    explicit = getattr(cfg, "services", ()) or ()
    if explicit:
        return ctx.json(
            200, {"available": True, "services": [d.state(n) for n in explicit]}
        )

    hunter_name = getattr(cfg, "hunter_container", "hunter") or "hunter"
    candidates = list(dict.fromkeys([
        hunter_name,
        getattr(cfg, "beets_container", "beets") or "beets",
        getattr(cfg, "slskd_container", "slskd") or "slskd",
        getattr(cfg, "navidrome_container", "navidrome") or "navidrome",
        getattr(cfg, "prowlarr_container", "prowlarr") or "prowlarr",
    ]))
    out = []
    for name in candidates:
        st = d.state(name)
        if st.get("exists") or name == hunter_name:
            out.append(st)
    return ctx.json(200, {"available": True, "services": out})

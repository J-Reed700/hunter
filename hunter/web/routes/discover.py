"""Discovery: MusicBrainz search, artist discographies, calendar, direct
Soulseek browse."""
from __future__ import annotations

import time

import requests

from hunter import mb
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log


def _ics_escape(val: str) -> str:
    """Escape text per RFC 5545 §3.3.11."""
    return (
        (val or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


@get("/api/search")
def search(ctx: Ctx):
    kind = ctx.q("kind", "album")
    q = ctx.q("q")
    if len(q) < 2:
        return ctx.error(400, "query too short")
    if kind not in ("album", "artist", "song"):
        return ctx.error(400, "bad kind")
    try:
        results = mb.search(kind, q, ctx.qi("offset", 0, 0))
        if kind != "artist":
            results = common.annotate_owned(ctx.cfg, results)
        return ctx.json(200, {"results": results})
    except mb.MBUnavailable as exc:
        log.info("mb search deferred: %s", exc)
        return ctx.json(503, {"error": "MusicBrainz is busy", "retry": True})
    except (requests.RequestException, ValueError) as exc:
        return ctx.error(502, f"musicbrainz: {exc}")


@get("/api/artist_albums")
def artist_albums(ctx: Ctx):
    artist_id, name = ctx.q("id"), ctx.q("name")
    if not artist_id:
        return ctx.error(400, "id required")
    try:
        albums = mb.artist_release_groups(artist_id)
        for a in albums:
            a["artist"] = name
        albums = common.annotate_owned(ctx.cfg, albums)
        with common.state(ctx.cfg) as st:
            wm = common.want_status_map(
                st, [f"request:rg:{a['rgid']}" for a in albums]
            )
        for a in albums:
            a["want"] = wm.get(f"request:rg:{a['rgid']}")
        return ctx.json(200, {"albums": albums})
    except mb.MBUnavailable:
        return ctx.json(503, {"error": "MusicBrainz is busy", "retry": True})
    except (requests.RequestException, ValueError) as exc:
        return ctx.error(502, f"musicbrainz: {exc}")


@get("/api/calendar")
def calendar(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 120 * 86_400))
        rows = [dict(r) for r in st.calendar_rows(since, "9999")]
    today = time.strftime("%Y-%m-%d")
    upcoming = [r for r in rows if r["date"][:10] > today]
    recent = [r for r in rows if r["date"][:10] <= today][::-1]
    common.annotate_owned(ctx.cfg, upcoming)
    common.annotate_owned(ctx.cfg, recent)
    return ctx.json(200, {"upcoming": upcoming, "recent": recent})


@get("/calendar.ics")
def calendar_ics(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        rows = st.calendar_rows(time.strftime("%Y-%m-%d"), "9999")
    ev = []
    for r in rows:
        d = (r["date"] + "-01-01")[:10].replace("-", "")
        uid = _ics_escape(r["rgid"])
        summary = f"{_ics_escape(r['artist'])} — {_ics_escape(r['album'])}"
        ev.append(
            f"BEGIN:VEVENT\r\nUID:{uid}@hunter\r\nDTSTART;VALUE=DATE:{d}"
            f"\r\nSUMMARY:{summary}\r\nEND:VEVENT"
        )
    body = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//hunter//releases//EN\r\n"
        + "\r\n".join(ev)
        + "\r\nEND:VCALENDAR\r\n"
    )
    return ctx.send(200, body.encode("utf-8"), "text/calendar; charset=utf-8")


@post("/api/slskd_search")
def slskd_search(ctx: Ctx):
    cfg = ctx.cfg
    q = ctx.b("q")
    if len(q) < 2:
        return ctx.error(400, "query too short")
    if not cfg.slskd_configured:
        return ctx.error(400, "slskd not configured")
    from hunter.slskd_client import SlskdClient, SlskdError, rank_candidates

    with common.state(cfg) as st:
        banned = st.banned_peers()
        rep = st.peer_reputation()
    try:
        with SlskdClient(cfg.slskd_url, cfg.slskd_api_key) as slskd:
            responses = slskd.search(q, min(cfg.search_timeout, 30))
    except SlskdError as exc:
        return ctx.error(502, f"slskd: {exc}")
    ranked = rank_candidates(
        responses,
        None,
        0.1,
        10.0,
        cfg.prefer_lossless,
        set(),
        min_bitrate=cfg.min_bitrate,
        banned=banned,
        min_files=1,
        reputation=rep,
    )
    out = [
        {
            "username": c.username,
            "dir": c.dir,
            "dir_leaf": c.dir.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1],
            "count": c.audio_count,
            "lossless": c.lossless,
            "bitrate": c.avg_bitrate,
            "queue": c.queue_length,
            "slot": c.has_free_slot,
            "size": sum(int(f.get("size") or 0) for f in c.files),
            "files": [
                {"filename": f.get("filename"), "size": f.get("size")}
                for f in c.files
            ][:120],
        }
        for c in ranked[:60]
    ]
    return ctx.json(200, {"candidates": out})


@post("/api/slskd_download")
def slskd_download(ctx: Ctx):
    cfg = ctx.cfg
    username, dir_, label = ctx.b("username"), ctx.b("dir"), ctx.b("label")
    files = ctx.body.get("files")
    if (
        not username
        or not isinstance(files, list)
        or not files
        or not all(isinstance(f, dict) and f.get("filename") for f in files)
    ):
        return ctx.error(400, "username + files required")
    from hunter.slskd_client import SlskdClient, SlskdError

    try:
        with SlskdClient(cfg.slskd_url, cfg.slskd_api_key) as slskd:
            slskd.enqueue(username, files)
    except SlskdError as exc:
        return ctx.error(502, f"slskd: {exc}")
    leaf = dir_.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or "download"
    key = f"browse:{username}:{leaf[:80]}"
    with common.state(cfg) as st:
        st.upsert_want(
            key, label or username, leaf, 0, len(files), "browse", resurrect=True
        )
        want_id = st.want_id_by_key(key)
        if want_id is not None:
            st.mark_grabbed(want_id, username, dir_)
        st.record_peer_grab(username)
    log.info("browse download: %s from %s (%d files)", leaf, username, len(files))
    return ctx.json(200, {"queued": len(files)})

"""Followed artists (release watch) + canon coverage."""
from __future__ import annotations

import requests

from hunter import mb
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log
_ALLOWED_WANT_TYPES = frozenset({"album", "ep", "single", "live"})


def _clean_want_types(raw: object) -> str:
    if raw is None:
        return "album,ep"
    cleaned = ",".join(
        t.strip() for t in str(raw).split(",") if t.strip() in _ALLOWED_WANT_TYPES
    )
    return cleaned or "album,ep"


@get("/api/artists")
def artists(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        rows = [dict(r) for r in st.followed_artists()]
        counts = st.open_wants_by_artist()
    for r in rows:
        r["open_wants"] = counts.get(r["name"], 0)
        r["want_types"] = r.get("want_types") or "album,ep"
    return ctx.json(
        200, {"artists": rows, "check_hours": ctx.cfg.artist_check_hours}
    )


@post("/api/follow")
def follow(ctx: Ctx):
    artist_id, name = ctx.b("id"), ctx.b("name")
    if not artist_id or not name:
        return ctx.error(400, "id and name required")
    wt = _clean_want_types(ctx.body.get("want_types"))
    with common.state(ctx.cfg) as st:
        added = st.follow_artist(artist_id, name)
        if added:
            st.log_event("follow", name, "", "via ui")
        st.set_artist_option(artist_id, None, wt)
    log.info("following artist: %s (%s)", name, artist_id)
    return ctx.json(200, {"added": added, "want_types": wt})


@post("/api/follow_by_name")
def follow_by_name(ctx: Ctx):
    name = ctx.b("name")
    if not name:
        return ctx.error(400, "name required")
    try:
        hits = mb.search("artist", name)
    except mb.MBUnavailable:
        return ctx.json(503, {"error": "MusicBrainz is busy", "retry": True})
    except (requests.RequestException, ValueError) as exc:
        return ctx.error(502, f"musicbrainz: {exc}")
    if not hits:
        return ctx.error(404, "no MusicBrainz artist match")
    hit = hits[0]
    with common.state(ctx.cfg) as st:
        added = st.follow_artist(hit["id"], hit["name"])
        if added:
            st.log_event("follow", hit["name"], "", "from library via ui")
    return ctx.json(
        200,
        {"added": added, "id": hit["id"], "name": hit["name"], "want_types": "album,ep"},
    )


@post("/api/artist_option")
def artist_option(ctx: Ctx):
    mbid = ctx.b("mbid")
    if not mbid:
        return ctx.error(400, "mbid required")
    monitored = ctx.body.get("monitored")
    raw_wt = ctx.body.get("want_types")
    want_types = _clean_want_types(raw_wt) if raw_wt is not None else None
    with common.state(ctx.cfg) as st:
        ok = st.set_artist_option(
            mbid,
            monitored if isinstance(monitored, bool) else None,
            want_types,
        )
        row = st.artist_by_mbid(mbid)
    return ctx.json(
        200 if ok else 404,
        {
            "updated": ok,
            "want_types": (row["want_types"] if row else want_types or "album,ep"),
        },
    )


@post("/api/unfollow")
def unfollow(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        removed = st.unfollow_artist(ctx.b("id"))
    return ctx.json(200, {"removed": removed})


@post("/api/artist_check")
def artist_check(ctx: Ctx):
    """Force a release check for one followed artist on the next pass."""
    mbid = ctx.b("mbid")
    with common.state(ctx.cfg) as st:
        ok = st.reset_artist_check(mbid)
    if ok:
        common.spawn_pass()
    return ctx.json(200 if ok else 404, {"queued": ok})


@get("/api/canon")
def canon(ctx: Ctx):
    cfg = ctx.cfg
    name = ctx.q("name") or ""
    mbid = ctx.q("mbid") or ""
    if not name and not mbid:
        return ctx.error(400, "name or mbid required")
    from hunter import listenbrainz as lb

    with common.state(cfg) as st:
        if not mbid:
            try:
                hits = mb.search("artist", name)
                mbid = hits[0]["id"] if hits else ""
            except (requests.RequestException, ValueError, mb.MBUnavailable):
                pass
        if not mbid:
            return ctx.json(200, {"canon": [], "note": "no MusicBrainz artist match"})
        if st.canon_age(mbid) > 604_800 or ctx.q("refresh") == "1":
            try:
                rgs = mb.artist_release_groups(mbid)
            except (requests.RequestException, ValueError, mb.MBUnavailable):
                rgs = []
            rgs = [r for r in rgs if not r.get("secondary")]
            scores, complete = lb.canon_scores_checked(mbid, rgs)
            if complete:
                with st.transaction():
                    for c in scores:
                        st.upsert_canon(
                            c["rgid"],
                            mbid,
                            name,
                            c["name"],
                            c.get("year") or 0,
                            c["score"],
                            "",
                        )
        rows = st.canon_for_artist(mbid)
        wm = common.want_status_map(st, [f"request:rg:{r['rgid']}" for r in rows])

    lib = common.library(cfg)
    out = []
    for r in rows:
        owned, have, album_id = False, 0, None
        if lib is not None:
            res = lib.rg_owned(r["rgid"], None, cfg.owned_coverage)
            if res is None:
                res = lib.owned(name, r["album"], None, cfg.owned_coverage)
            owned, have, album_id = res.owned, res.item_count, res.album_id
        out.append(
            {
                "rgid": r["rgid"],
                "album": r["album"],
                "year": r["year"],
                "score": r["score"],
                "owned": owned,
                "have": have,
                "album_id": album_id,
                "want": wm.get(f"request:rg:{r['rgid']}"),
            }
        )
    return ctx.json(
        200,
        {
            "canon": out,
            "mbid": mbid,
            "owned": sum(1 for x in out if x["owned"]),
            "total": len(out),
        },
    )

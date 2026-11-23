"""Library manager — the beets DB as a browsable, actionable catalogue."""
from __future__ import annotations

import shlex

from hunter import beetcmd, beetsops, blocklist, domain, libops, libquery
from hunter.dockerctl import DockerClient
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log


@get("/api/library/overview")
def overview(ctx: Ctx):
    return ctx.json(200, libquery.overview(ctx.cfg))


def _album_filters(ctx: Ctx) -> dict:
    return dict(
        q=ctx.q("q"),
        artist=ctx.q("artist"),
        genre=ctx.q("genre"),
        label=ctx.q("label"),
        year=ctx.qi("year", 0) or None,
        decade=ctx.qi("decade", 0) or None,
        year_from=ctx.qi("year_from", 0) or None,
        year_to=ctx.qi("year_to", 0) or None,
        filt=ctx.q("filter"),
        fmt=ctx.q("fmt"),
        albumtype=ctx.q("type"),
        added=ctx.q("added"),
        letter=ctx.q("letter"),
        min_tracks=ctx.qi("min_tracks", 0) or None,
        max_tracks=ctx.qi("max_tracks", 0) or None,
    )


@get("/api/library/albums")
def albums(ctx: Ctx):
    res = libquery.albums(
        ctx.cfg,
        sort=ctx.q("sort", "added"),
        offset=ctx.qi("offset", 0, 0),
        limit=ctx.qi("limit", 60, 1, 500),
        **_album_filters(ctx),
    )
    _attach_wants(ctx, res["albums"])
    return ctx.json(200, res)


@get("/api/library/albums/ids")
def album_ids(ctx: Ctx):
    return ctx.json(
        200,
        libquery.album_ids(
            ctx.cfg,
            cap=ctx.qi("cap", 5000, 1, 20000),
            **_album_filters(ctx),
        ),
    )


@get("/api/library/albums/groups")
def album_groups(ctx: Ctx):
    return ctx.json(
        200,
        libquery.album_groups(
            ctx.cfg,
            by=ctx.q("by", "genre"),
            limit=ctx.qi("limit", 400, 1, 2000),
            **_album_filters(ctx),
        ),
    )


def _attach_wants(ctx: Ctx, rows: list[dict]) -> None:
    keys = []
    for r in rows:
        keys += [f"gapfill:beets:{r['id']}", f"upgrade:beets:{r['id']}"]
        if r.get("rgid"):
            keys.append(f"request:rg:{r['rgid']}")
    with common.state(ctx.cfg) as st:
        wm = st.wants_status_map(keys)
    for r in rows:
        r["wants"] = {
            k: v
            for k, v in (
                ("gapfill", wm.get(f"gapfill:beets:{r['id']}")),
                ("upgrade", wm.get(f"upgrade:beets:{r['id']}")),
                ("request", wm.get(f"request:rg:{r['rgid']}") if r.get("rgid") else None),
            )
            if v
        }


@get("/api/library/album")
def album(ctx: Ctx):
    res = libquery.album(ctx.cfg, ctx.qi("id"))
    if not res:
        return ctx.error(404, "unknown album")
    _attach_wants(ctx, [res])
    if ctx.user and ctx.user.can("operator"):
        jobs = common.jobs(ctx.cfg)
        res["jobs"] = [
            j for j in jobs.list(limit=30) if j["meta"].get("album_id") == res["id"]
        ][:6]
    else:
        res["jobs"] = []
    return ctx.json(200, res)


@get("/api/library/album_art")
def album_art(ctx: Ctx):
    p = libquery.album_art_path(ctx.cfg, ctx.qi("id"))
    if p is None:
        return ctx.error(404, "no art")
    return ctx.file(p, cache="private, max-age=86400")


def _artist_filters(ctx: Ctx) -> dict:
    """Resolve hunter-side filters (`followed`/`blocked`) using normalized matching
    so filtered results match the displayed badges."""
    filt = ctx.q("filter")
    parts = [f.strip() for f in filt.split(",") if f.strip()]
    names_in: list[str] | None = None
    names_not_in: list[str] | None = None
    hunter_flags = {"followed", "unfollowed", "blocked", "unblocked"}
    if any(f in hunter_flags for f in parts):
        with common.state(ctx.cfg) as st:
            followed_cf = {r["name"].casefold() for r in st.followed_artists()}
        snap = blocklist.snapshot(ctx.cfg)
        all_names = [
            a["name"]
            for a in libquery.artist_names(ctx.cfg, cap=50000)["artists"]
        ]
        matched_followed = [n for n in all_names if n.casefold() in followed_cf]
        matched_blocked = [n for n in all_names if domain.blocklist_norm(n) in snap.keys]

        if "followed" in parts:
            names_in = matched_followed
        if "blocked" in parts:
            if names_in is None:
                names_in = matched_blocked
            else:
                bset = set(matched_blocked)
                names_in = [n for n in names_in if n in bset]
        excl: list[str] = []
        if "unfollowed" in parts:
            excl.extend(matched_followed)
        if "unblocked" in parts:
            excl.extend(matched_blocked)
        names_not_in = excl or None
        parts = [f for f in parts if f not in hunter_flags]
    return dict(
        q=ctx.q("q"),
        letter=ctx.q("letter"),
        genre=ctx.q("genre"),
        decade=ctx.qi("decade", 0) or None,
        filt=",".join(parts),
        added=ctx.q("added"),
        min_albums=ctx.qi("min_albums", 0) or None,
        max_albums=ctx.qi("max_albums", 0) or None,
        names_in=names_in,
        names_not_in=names_not_in,
    )


@get("/api/library/artists")
def artists(ctx: Ctx):
    res = libquery.artists(
        ctx.cfg,
        offset=ctx.qi("offset", 0, 0),
        limit=ctx.qi("limit", 100, 1, 500),
        sort=ctx.q("sort", "name"),
        **_artist_filters(ctx),
    )
    with common.state(ctx.cfg) as st:
        followed = {r["name"].casefold(): r["mbid"] for r in st.followed_artists()}
    blocked = blocklist.blocked_set([r["name"] for r in res["artists"]], ctx.cfg)
    for r in res["artists"]:
        r["followed"] = r["name"].casefold() in followed
        r["followed_mbid"] = followed.get(r["name"].casefold(), "")
        r["blocked"] = r["name"] in blocked
    return ctx.json(200, res)


@get("/api/library/artists/names")
def artist_names(ctx: Ctx):
    return ctx.json(
        200,
        libquery.artist_names(
            ctx.cfg,
            cap=ctx.qi("cap", 5000, 1, 20000),
            **_artist_filters(ctx),
        ),
    )


@get("/api/library/artists/groups")
def artist_groups(ctx: Ctx):
    return ctx.json(
        200,
        libquery.artist_groups(
            ctx.cfg,
            by=ctx.q("by", "genre"),
            limit=ctx.qi("limit", 400, 1, 2000),
            **_artist_filters(ctx),
        ),
    )


@get("/api/library/artist")
def artist(ctx: Ctx):
    name = ctx.q("name")
    if not name:
        return ctx.error(400, "name required")
    res = libquery.artist(ctx.cfg, name)
    if not res:
        return ctx.error(404, "unknown artist")
    _attach_wants(ctx, res["albums"])
    with common.state(ctx.cfg) as st:
        f = next(
            (
                dict(r)
                for r in st.followed_artists()
                if r["name"].casefold() == name.casefold()
            ),
            None,
        )
        res["open_wants"] = st.open_wants_count_for_artists([name])
    res["followed"] = f
    res["blocked"] = blocklist.is_blocked(name, ctx.cfg)
    return ctx.json(200, res)


def _track_filters(ctx: Ctx) -> dict:
    return dict(
        q=ctx.q("q"),
        fmt=ctx.q("fmt"),
        genre=ctx.q("genre"),
        album_id=ctx.qi("album_id", 0) or None,
        artist=ctx.q("artist"),
        year=ctx.qi("year", 0) or None,
        decade=ctx.qi("decade", 0) or None,
        filt=ctx.q("filter"),
        added=ctx.q("added"),
        min_kbps=ctx.qi("min_kbps", 0) or None,
        max_kbps=ctx.qi("max_kbps", 0) or None,
    )


@get("/api/library/tracks")
def tracks(ctx: Ctx):
    return ctx.json(
        200,
        libquery.tracks(
            ctx.cfg,
            sort=ctx.q("sort", "added"),
            offset=ctx.qi("offset", 0, 0),
            limit=ctx.qi("limit", 100, 1, 500),
            **_track_filters(ctx),
        ),
    )


@get("/api/library/tracks/ids")
def track_ids(ctx: Ctx):
    return ctx.json(
        200,
        libquery.track_ids(
            ctx.cfg,
            cap=ctx.qi("cap", 5000, 1, 20000),
            **_track_filters(ctx),
        ),
    )


@get("/api/library/tracks/groups")
def track_groups(ctx: Ctx):
    return ctx.json(
        200,
        libquery.track_groups(
            ctx.cfg,
            by=ctx.q("by", "genre"),
            limit=ctx.qi("limit", 400, 1, 2000),
            **_track_filters(ctx),
        ),
    )


@get("/api/library/duplicates")
def duplicates(ctx: Ctx):
    return ctx.json(
        200,
        libquery.duplicates(
            ctx.cfg,
            q=ctx.q("q"),
            scope=ctx.q("scope", "all"),
            offset=ctx.qi("offset", 0, 0),
            limit=ctx.qi("limit", 60, 1, 300),
            sort=ctx.q("sort", "waste"),
        ),
    )


@get("/api/library/junk")
def junk(ctx: Ctx):
    return ctx.json(200, libquery.junk(ctx.cfg))


@get("/api/library/track")
def track(ctx: Ctx):
    res = libquery.track(ctx.cfg, ctx.qi("id"))
    if not res:
        return ctx.error(404, "unknown track")
    return ctx.json(200, res)


@get("/api/library/genres")
def genres(ctx: Ctx):
    return ctx.json(
        200,
        {
            "genres": libquery.genres(ctx.cfg, ctx.qi("limit", 200, 1, 2000)),
            "types": libquery.albumtypes(ctx.cfg),
        },
    )


# ---- actions ---------------------------------------------------------------

@post("/api/library/want")
def want(ctx: Ctx):
    """Turn a library album into a want: complete (gapfill) or upgrade."""
    cfg = ctx.cfg
    album_id = ctx.bi("album_id")
    kind = ctx.b("kind") or "gapfill"
    if not album_id or kind not in ("gapfill", "upgrade"):
        return ctx.error(400, "album_id + kind (gapfill|upgrade)")
    a = libquery.album(cfg, album_id)
    if not a:
        return ctx.error(404, "unknown album")
    if blocklist.is_blocked(a["artist"], cfg):
        return ctx.error(409, f"artist {a['artist']!r} is on the import blocklist")
    total = a.get("tracktotal") or ctx.bi("expected") or None
    if kind == "gapfill" and total and a["tracks"] >= total:
        return ctx.json(
            200, {"added": False, "detail": "album is already complete"}
        )
    key = f"{kind}:beets:{album_id}"
    with common.state(cfg) as st:
        added = st.upsert_want(
            key,
            a["artist"],
            a["album"],
            a.get("year") or 0,
            total,
            kind,
            resurrect=True,
            cfg=cfg,
        )
        if added:
            st.set_priority(key, domain.PRIORITY_OPERATOR)
            st.log_event(
                "request", a["artist"], a["album"], f"{kind} from library via ui"
            )
    if added:
        common.spawn_pass()
    return ctx.json(200, {"added": bool(added)})


@post("/api/library/beet")
def beet_action(ctx: Ctx):
    cfg = ctx.cfg
    action = ctx.b("action")
    album_id = ctx.bi("album_id")
    item_id = ctx.bi("item_id")
    artist_name = ctx.b("artist")
    allowed = {
        "fetchart",
        "embedart",
        "write",
        "mbsync",
        "move",
        "update",
        "replaygain",
        "lyrics",
        "lastgenre",
        "scrub",
        "fingerprint",
        "clearart",
        "ftintitle",
    }
    if action not in allowed:
        return ctx.error(400, f"action must be one of {sorted(allowed)}")
    if album_id:
        query, label = f"album_id:{album_id}", f"album {album_id}"
        a = libquery.album(cfg, album_id)
        if a:
            label = f"{a['artist']} — {a['album']}"
    elif item_id:
        query, label = f"id:{item_id}", f"track {item_id}"
    elif artist_name:
        # Quote safely so apostrophes/spaces (e.g. Guns N' Roses) stay one token.
        query, label = shlex.quote(f"albumartist:{artist_name}"), artist_name
    else:
        return ctx.error(400, "album_id, item_id or artist required")
    opts: dict = {}
    if ctx.bb("force"):
        opts["-f"] = True
    if not item_id and action in ("lastgenre", "replaygain"):
        opts["-a"] = True
    try:
        argv = beetcmd.build(action, query=query, options=opts, apply=True)
    except beetcmd.PolicyError as exc:
        return ctx.error(400, str(exc))
    if cfg.beets_console_available:
        err = beetsops.guard_mutation(
            cfg,
            DockerClient(cfg.docker_sock),
            argv,
            allow_all=ctx.bb("allow_all"),
            confirm_count=ctx.bi("confirm_count", -1),
        )
        if err is not None:
            return ctx.json(err[0], err[1])
    res = common.jobs(cfg).start_beets(
        kind="beet",
        title=f"{action} · {label}",
        argv=beetcmd.beet_argv(cfg) + argv,
        mode="apply",
        meta={
            "album_id": album_id or None,
            "item_id": item_id or None,
            "artist": artist_name or None,
            "query": query,
            "cmd": action,
        },
        exclusive=f"beet:{action}:{query}",
    )
    if res["ok"]:
        common.invalidate_library()
    return ctx.json(200 if res["ok"] else 409, res)


@post("/api/library/modify")
def modify(ctx: Ctx):
    """Guarded `beet modify`: enforces blast-radius count + confirm_count on apply."""
    cfg = ctx.cfg
    album_id = ctx.bi("album_id")
    item_ids = ctx.body.get("item_ids")
    query = ctx.b("query")
    mods = ctx.body.get("mods")
    album_mode = ctx.bb("album")
    if album_id:
        query = f"album_id:{album_id}"
    elif isinstance(item_ids, list) and item_ids:
        ids = [int(i) for i in item_ids if isinstance(i, int) and not isinstance(i, bool)][:500]
        query = " , ".join(f"id:{i}" for i in ids)
    if not query:
        return ctx.error(400, "query, album_id or item_ids required")
    if not isinstance(mods, dict) or not mods:
        return ctx.error(400, "mods {field: value} required")
    positional = []
    for k, v in mods.items():
        if not str(k).replace("_", "").isalnum():
            return ctx.error(400, f"bad field {k!r}")
        if v is None or v == "":
            positional.append(f"{k}!")
        else:
            positional.append(f"{k}={v}")
    opts = {
        "-a": album_mode,
        "-M": ctx.bb("no_move"),
        "-W": ctx.bb("no_write"),
    }
    apply = ctx.bb("apply")
    try:
        argv = beetcmd.build(
            "modify", query=query, options=opts, positional=positional, apply=apply
        )
    except (beetcmd.PolicyError, ValueError) as exc:
        return ctx.error(400, str(exc))

    if apply:
        if not cfg.beets_console_available:
            return ctx.error(503, "docker socket not mounted")
        err = beetsops.guard_mutation(
            cfg,
            DockerClient(cfg.docker_sock),
            argv,
            allow_all=ctx.bb("allow_all"),
            confirm_count=ctx.bi("confirm_count", -1),
        )
        if err is not None:
            return ctx.json(err[0], err[1])

    label = query if len(query) < 60 else query[:57] + "…"
    res = common.jobs(cfg).start_beets(
        kind="beet",
        title=f"modify · {label}",
        argv=beetcmd.beet_argv(cfg) + argv,
        mode="apply" if apply else "dry-run",
        meta={
            "album_id": album_id or None,
            "query": query,
            "mods": mods,
            "cmd": "modify",
        },
        exclusive=f"beet:modify:{query}",
    )
    if res["ok"] and apply:
        common.invalidate_library()
    return ctx.json(200 if res["ok"] else 409, res)


# ---- removal (journaled, undoable) --------------------------------------------

def _targets(ctx: Ctx) -> tuple[list[str], list[int], list[int]]:
    return (
        ctx.blist("artists", str, 50),
        ctx.blist("album_ids", int, 2000),
        ctx.blist("item_ids", int, libops.HUGE + 1),
    )


@post("/api/library/remove/plan", audit=False)
def remove_plan(ctx: Ctx):
    artists_t, album_ids_t, item_ids_t = _targets(ctx)
    if not (artists_t or album_ids_t or item_ids_t):
        return ctx.error(400, "artists, album_ids or item_ids required")
    plan = libops.removal_plan(ctx.cfg, artists_t, album_ids_t, item_ids_t)
    with common.state(ctx.cfg) as st:
        names = plan["targets"]["artists"]
        keys = libops.plan_park_keys(plan)
        plan["open_wants"] = len(st.wants_for_artists_and_keys(names, keys))
        low = {n.casefold() for n in names}
        plan["followed"] = [
            r["name"]
            for r in st.followed_artists()
            if r["name"].casefold() in low
        ]
    plan["console"] = ctx.cfg.beets_console_available
    plan["preflight"] = (
        libops.removal_preflight(ctx.cfg, need_bytes=plan.get("est_bytes", 0))
        if plan["console"]
        else None
    )
    return ctx.json(200, plan)


@post("/api/library/remove")
def remove(ctx: Ctx):
    if not ctx.cfg.beets_console_available:
        return ctx.error(503, "docker socket not mounted")
    artists_t, album_ids_t, item_ids_t = _targets(ctx)
    if not (artists_t or album_ids_t or item_ids_t):
        return ctx.error(400, "artists, album_ids or item_ids required")
    plan = libops.removal_plan(ctx.cfg, artists_t, album_ids_t, item_ids_t)
    if plan["empty"]:
        return ctx.error(400, "nothing matched — exact names/ids only")
    pf = libops.removal_preflight(
        ctx.cfg, force=True, need_bytes=plan.get("est_bytes", 0)
    )
    if not pf.get("ok"):
        return ctx.json(
            409,
            {
                "error": f"storage is not ready in the beets container — {pf.get('error')}",
                "preflight": pf,
            },
        )
    raw_confirm = ctx.body.get("confirm")
    if raw_confirm is not None and not isinstance(raw_confirm, dict):
        return ctx.error(400, "confirm must be an object")
    confirm = raw_confirm if isinstance(raw_confirm, dict) else {}
    raw_tracks = confirm.get("tracks")
    if raw_tracks is None:
        confirm_tracks = -1
    elif isinstance(raw_tracks, bool) or not isinstance(raw_tracks, (int, str)):
        return ctx.error(400, "confirm.tracks must be an integer")
    else:
        try:
            confirm_tracks = int(raw_tracks)
        except ValueError:
            return ctx.error(400, "confirm.tracks must be an integer")
    if (
        confirm.get("fingerprint") != plan["fingerprint"]
        or confirm_tracks != plan["tracks"]
    ):
        return ctx.json(
            409,
            {
                "error": "the library changed since the preview — review again",
                "plan": {k: v for k, v in plan.items() if k != "albums"},
            },
        )
    if plan["too_many"] and not ctx.bb("allow_huge"):
        return ctx.error(
            400,
            f"{plan['tracks']} tracks exceeds {libops.HUGE}; confirm allow_huge to proceed",
        )
    res = libops.start_removal(
        ctx.cfg,
        common.jobs(ctx.cfg),
        plan,
        block=ctx.bb("block"),
        reason=ctx.b("reason"),
        requested_by=ctx.username,
        drop_wants=ctx.body.get("drop_wants", True) is not False,
        unfollow=ctx.body.get("unfollow", True) is not False,
        allow_huge=ctx.bb("allow_huge"),
    )
    if res["ok"]:
        log.info(
            "library removal started by %s: %s", ctx.username, res["job"]["title"]
        )
    return ctx.json(200 if res["ok"] else 409, res)


@get("/api/library/removals")
def removals(ctx: Ctx):
    is_op = bool(ctx.user and ctx.user.can("operator"))
    return ctx.json(
        200,
        {
            "removals": libops.removals(ctx.cfg, ctx.qi("limit", 60, 1, 200)),
            "root": str(libops.prune_root(ctx.cfg)) if is_op else "",
        },
    )


@get("/api/library/removal")
def removal(ctx: Ctx):
    res = libops.removal(ctx.cfg, ctx.q("id"))
    if not res:
        return ctx.error(404, "unknown removal")
    if ctx.user and ctx.user.can("operator"):
        jobs = common.jobs(ctx.cfg)
        res["jobs"] = [
            j
            for j in jobs.list(limit=60, kind="library")
            if j["meta"].get("removal") == res["id"]
        ][:5]
    else:
        res["jobs"] = []
    return ctx.json(200, res)


@post("/api/library/removal/undo")
def removal_undo(ctx: Ctx):
    if not ctx.cfg.beets_console_available:
        return ctx.error(503, "docker socket not mounted")
    pf = libops.removal_preflight(ctx.cfg, force=True)
    if not pf.get("ok"):
        return ctx.json(
            409,
            {
                "error": f"storage is not ready in the beets container — {pf.get('error')}",
                "preflight": pf,
            },
        )
    res = libops.start_undo(
        ctx.cfg, common.jobs(ctx.cfg), ctx.b("id"), ctx.username
    )
    return ctx.json(200 if res["ok"] else 409, res)


@get("/api/library/job_result", role="operator")
def job_result(ctx: Ctx):
    job = common.jobs(ctx.cfg).get(ctx.q("id"))
    if not job:
        return ctx.error(404, "unknown job")
    return ctx.json(200, {"job": job, "result": libops.job_result(job)})


# ---- blocklist -------------------------------------------------------------------

@get("/api/library/blocklist")
def blocklist_get(ctx: Ctx):
    return ctx.json(200, libops.blocklist_view(ctx.q("q"), ctx.cfg))


@get("/api/library/blocklist/check")
def blocklist_check(ctx: Ctx):
    name = ctx.q("name")
    return ctx.json(
        200, {"name": name, "blocked": blocklist.is_blocked(name, ctx.cfg)}
    )


@post("/api/library/blocklist")
def blocklist_post(ctx: Ctx):
    add, remove_ = ctx.blist("add", str, 200), ctx.blist("remove", str, 200)
    if not (add or remove_):
        return ctx.error(400, "add or remove required")
    res = libops.blocklist_edit(
        ctx.cfg, DockerClient(ctx.cfg.docker_sock), add=add, remove=remove_
    )
    if not res.get("ok"):
        return ctx.json(502, res)
    res["wants_parked"] = libops.blocklist_delta_for_wants(
        ctx.cfg, res.get("added", [])
    )
    with common.state(ctx.cfg) as st:
        for n in res.get("added", []):
            st.log_event(
                "blocked",
                n,
                "",
                f"added to the import blocklist by {ctx.username}",
            )
        for n in res.get("removed", []):
            st.log_event(
                "unblocked",
                n,
                "",
                f"removed from the import blocklist by {ctx.username}",
            )
    return ctx.json(200, res)

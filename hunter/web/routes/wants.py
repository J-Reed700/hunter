"""The want queue: list, request, add, drop, retry, candidates, grab."""
from __future__ import annotations

import re

import requests

from hunter import blocklist, domain, mb
from hunter.domain import norm_album, norm_artist
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log
_MAX_ENQUEUE_FILES = 500


@get("/api/wants")
def wants(ctx: Ctx):
    statuses = [s for s in (ctx.q("status") or "").split(",") if s]
    source = ctx.q("source")
    text = ctx.q("q")
    with common.state(ctx.cfg) as st:
        rows = st.wants_query(
            statuses=statuses,
            source=source,
            text=text,
            sort=ctx.q("sort", "updated"),
            limit=ctx.qi("limit", 100, 1, 1000),
            offset=ctx.qi("offset", 0, 0),
        )
        total = st.wants_count_filtered(statuses=statuses, source=source, text=text)
        sources = st.want_sources()
    return ctx.json(
        200, {"wants": [dict(r) for r in rows], "total": total, "sources": sources}
    )


@get("/api/want")
def want(ctx: Ctx):
    wid = ctx.qi("id")
    with common.state(ctx.cfg) as st:
        row = st.want_row(wid)
        if row is None:
            return ctx.error(404, "unknown want")
        attempts = [dict(r) for r in st.attempt_history(wid)]
        events = [dict(r) for r in st.want_events(row["artist"], row["album"], 30)]
    lib = common.library(ctx.cfg)
    owned = None
    if lib is not None:
        rgid = domain.extract_rgid_from_key(row["spotify_album_id"])
        res = lib.lookup_owned(
            row["artist"],
            row["album"],
            rgid=rgid,
            expected_tracks=row.get("track_count"),
            min_coverage=ctx.cfg.owned_coverage,
        )
        owned = {
            "owned": res.owned,
            "have": res.item_count,
            "expected": res.expected,
            "album_id": res.album_id,
        }
    return ctx.json(
        200,
        {"want": row, "attempts": attempts, "events": events, "owned": owned},
    )


@get("/api/attempts")
def attempts(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        return ctx.json(
            200, {"attempts": [dict(r) for r in st.attempt_history(ctx.qi("id"))]}
        )


@post("/api/request")
def request(ctx: Ctx):
    cfg = ctx.cfg
    rgid, artist, album = ctx.b("rgid"), ctx.b("artist"), ctx.b("album")
    year = max(0, ctx.bi("year"))
    if not rgid or not artist or not album:
        return ctx.error(400, "rgid, artist, album required")
    if blocklist.is_blocked(artist, cfg):
        return ctx.error(409, f"artist {artist!r} is on the import blocklist")
    if not ctx.bb("force"):
        have = common.already_have(cfg, artist, album, rgid=rgid)
        if have:
            log.info(
                "request skipped (owned): %s — %s (%d/%d)",
                artist,
                album,
                have.item_count,
                have.expected,
            )
            return ctx.json(200, {"added": False, **common.owned_payload(have)})
    key = f"request:rg:{rgid}"
    with common.state(cfg) as st:
        added = st.upsert_want(
            key, artist, album, year, None, "request", resurrect=True, cfg=cfg
        )
        if added:
            st.log_event("request", artist, album, "via ui")
    if added:
        common.fetch_counts_async(cfg, [(key, rgid)])
        common.spawn_pass()
        log.info("request queued: %s — %s -> pass spawned", artist, album)
    return ctx.json(200, {"added": added})


@post("/api/request_artist")
def request_artist(ctx: Ctx):
    cfg = ctx.cfg
    artist_id, name = ctx.b("id"), ctx.b("name")
    if not artist_id or not name:
        return ctx.error(400, "id and name required")
    if blocklist.is_blocked(name, cfg):
        return ctx.error(409, f"artist {name!r} is on the import blocklist")
    try:
        albums = mb.artist_release_groups(artist_id)
    except mb.MBUnavailable:
        return ctx.json(503, {"error": "MusicBrainz is busy", "retry": True})
    except (requests.RequestException, ValueError) as exc:
        return ctx.error(502, f"musicbrainz: {exc}")
    raw_types = ctx.body.get("types")
    types = (
        {str(t).strip().lower() for t in raw_types if t}
        if isinstance(raw_types, list)
        else {"album", "ep"}
    )
    force = ctx.bb("force")
    added = skipped = 0
    with common.state(cfg) as st:
        for a in albums:
            if types and (a.get("type") or "album").lower() not in types:
                continue
            if a.get("secondary"):
                continue
            if not force and common.already_have(
                cfg, name, a["album"], rgid=a["rgid"]
            ):
                skipped += 1
                continue
            added += st.upsert_want(
                f"request:rg:{a['rgid']}",
                name,
                a["album"],
                a["year"],
                None,
                "request",
                resurrect=True,
                cfg=cfg,
            )
    if added:
        common.spawn_pass()
    log.info(
        "artist request: %s -> %d queued, %d already owned", name, added, skipped
    )
    return ctx.json(
        200, {"added": added, "total": len(albums), "skipped_owned": skipped}
    )


@post("/api/request_bulk")
def request_bulk(ctx: Ctx):
    cfg = ctx.cfg
    items = ctx.body.get("items")
    if not isinstance(items, list) or not items or len(items) > 200:
        return ctx.error(400, "items: 1-200 required")
    force = ctx.bb("force")
    backfill: list[tuple[str, str]] = []
    skipped = added = blocked = 0
    with common.state(cfg) as st:
        for it in items:
            if not isinstance(it, dict):
                continue
            rgid = str(it.get("rgid", "")).strip()
            artist = str(it.get("artist", "")).strip()
            album = str(it.get("album", "")).strip()
            if not rgid or not artist or not album:
                continue
            if blocklist.is_blocked(artist, cfg):
                blocked += 1
                continue
            if not force and common.already_have(cfg, artist, album, rgid=rgid):
                skipped += 1
                continue
            try:
                year = max(0, int(it.get("year") or 0))
            except (TypeError, ValueError):
                year = 0
            key = f"request:rg:{rgid}"
            if st.upsert_want(
                key, artist, album, year, None, "request", resurrect=True, cfg=cfg
            ):
                added += 1
                backfill.append((key, rgid))
                st.log_event("request", artist, album, "bulk via ui")
    if backfill:
        common.fetch_counts_async(cfg, backfill)
    if added:
        common.spawn_pass()
    return ctx.json(
        200, {"added": added, "skipped_owned": skipped, "skipped_blocked": blocked}
    )


@post("/api/add")
def add(ctx: Ctx):
    cfg = ctx.cfg
    artist, album = ctx.b("artist"), ctx.b("album")
    if not artist or not album:
        return ctx.error(400, "artist and album required")
    if blocklist.is_blocked(artist, cfg):
        return ctx.error(409, f"artist {artist!r} is on the import blocklist")
    year = ctx.bi("year")
    tracks = ctx.bi("tracks")
    if year < 0 or tracks < 0:
        return ctx.error(400, "year and tracks must be non-negative")
    if not ctx.bb("force"):
        have = common.already_have(cfg, artist, album)
        if have:
            return ctx.json(200, {"added": False, **common.owned_payload(have)})
    with common.state(cfg) as st:
        new = st.upsert_want(
            f"manual:{norm_artist(artist)}:{norm_album(album)}",
            artist,
            album,
            year,
            tracks or None,
            "manual",
            resurrect=True,
            cfg=cfg,
        )
        if new:
            st.log_event("request", artist, album, "manual add via ui")
    if new:
        common.spawn_pass()
    return ctx.json(200, {"added": bool(new)})


@post("/api/drop")
def drop(ctx: Ctx):
    want_id = ctx.body.get("id")
    ids = ctx.body.get("ids")
    if isinstance(ids, list):
        ids = [int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)][:500]
    elif isinstance(want_id, int) and not isinstance(want_id, bool):
        ids = [want_id]
    else:
        return ctx.error(400, "id or ids required")
    with common.state(ctx.cfg) as st:
        with st.transaction():
            for i in ids:
                st.set_status(i, "skipped", "dropped via ui")
    return ctx.json(200, {"dropped": len(ids)})


@post("/api/drop_source")
def drop_source(ctx: Ctx):
    src = ctx.b("source")
    if not src:
        return ctx.error(400, "source required")
    with common.state(ctx.cfg) as st:
        n = st.drop_wants("%", "%", src, literal=True)
        st.log_event(
            "automation", detail=f"source {src!r} CLEARED via ui: {n} wants skipped"
        )
    log.info("drop_source via ui: %r -> %d wants skipped", src, n)
    return ctx.json(200, {"dropped": n, "source": src})


@post("/api/retry")
def retry(ctx: Ctx):
    want_id = ctx.body.get("id")
    ids = ctx.body.get("ids")
    if isinstance(ids, list):
        ids = [int(i) for i in ids if isinstance(i, int) and not isinstance(i, bool)][:200]
    elif isinstance(want_id, int) and not isinstance(want_id, bool):
        ids = [want_id]
    else:
        return ctx.error(400, "id required")
    n = 0
    with common.state(ctx.cfg) as st:
        for i in ids:
            n += 1 if st.retry_now(i) else 0
    if n:
        common.spawn_pass()
    return ctx.json(200 if n else 404, {"retried": n})


@post("/api/want/priority")
def priority(ctx: Ctx):
    want_id = ctx.body.get("id")
    prio = ctx.bi("priority", 0)
    if not isinstance(want_id, int) or isinstance(want_id, bool):
        return ctx.error(400, "id required")
    clamped = max(domain.PRIORITY_MIN, min(prio, domain.PRIORITY_MAX))
    with common.state(ctx.cfg) as st:
        if not st.set_priority_by_id(want_id, clamped):
            return ctx.error(404, "unknown want")
    return ctx.json(200, {"priority": clamped})


@post("/api/want/edit")
def edit(ctx: Ctx):
    """Correct a want's artist/album/track_count."""
    want_id = ctx.body.get("id")
    if not isinstance(want_id, int) or isinstance(want_id, bool):
        return ctx.error(400, "id required")
    kwargs: dict = {}
    if ctx.b("artist"):
        kwargs["artist"] = ctx.b("artist")
    if ctx.b("album"):
        kwargs["album"] = ctx.b("album")
    if "track_count" in ctx.body:
        tc = ctx.bi("track_count")
        if tc < 0:
            return ctx.error(400, "track_count must be non-negative")
        kwargs["track_count"] = tc or None
    if "year" in ctx.body:
        yr = ctx.bi("year")
        if yr < 0:
            return ctx.error(400, "year must be non-negative")
        kwargs["year"] = yr
    if not kwargs:
        return ctx.error(400, "nothing to change")
    with common.state(ctx.cfg) as st:
        if not st.update_want_fields(want_id, **kwargs):
            return ctx.error(404, "unknown want")
    return ctx.json(200, {"updated": True})


@post("/api/candidates")
def candidates(ctx: Ctx):
    cfg = ctx.cfg
    want_id = ctx.body.get("id")
    if not isinstance(want_id, int) or isinstance(want_id, bool):
        return ctx.error(400, "id required")
    if not cfg.slskd_configured:
        return ctx.error(400, "slskd not configured")
    with common.state(cfg) as st:
        want_obj = st.get_want(want_id)
        tried = st.tried_candidates(want_id)
        banned = st.banned_peers()
        rep = st.peer_reputation()
    if want_obj is None:
        return ctx.error(404, "unknown want")
    from hunter.pipeline import search_texts_for_want
    from hunter.slskd_client import (
        SlskdClient,
        SlskdError,
        candidate_to_dict,
        rank_candidates,
    )

    texts = (
        [ctx.b("text")]
        if ctx.b("text")
        else [t for t, _ in search_texts_for_want(want_obj)]
    )
    ranked: list = []
    used = ""
    try:
        with SlskdClient(cfg.slskd_url, cfg.slskd_api_key) as slskd:
            for text in texts:
                responses = slskd.search(text, min(cfg.search_timeout, 30))
                ranked = rank_candidates(
                    responses,
                    want_obj.track_count,
                    cfg.min_candidate_ratio,
                    cfg.max_candidate_ratio,
                    cfg.prefer_lossless,
                    tried,
                    min_bitrate=cfg.min_bitrate,
                    banned=banned,
                    reputation=rep,
                )
                used = text
                if ranked:
                    break
    except SlskdError as exc:
        return ctx.error(502, f"slskd: {exc}")
    out = [candidate_to_dict(c, max_files=80, tried=tried) for c in ranked[:30]]
    return ctx.json(200, {"candidates": out, "query": used})


@post("/api/grab")
def grab(ctx: Ctx):
    cfg = ctx.cfg
    if not cfg.slskd_configured:
        return ctx.error(400, "slskd not configured")
    want_id = ctx.body.get("id")
    username, dir_ = ctx.b("username"), ctx.b("dir")
    files = ctx.body.get("files")
    if (
        not isinstance(want_id, int)
        or isinstance(want_id, bool)
        or not username
        or not isinstance(files, list)
        or not files
        or len(files) > _MAX_ENQUEUE_FILES
        or not all(isinstance(f, dict) and f.get("filename") for f in files)
    ):
        return ctx.error(400, f"id, username, and 1-{_MAX_ENQUEUE_FILES} files required")
    with common.state(cfg) as st:
        w = st.get_want(want_id)
        if w is None:
            return ctx.error(404, "unknown want")
        if w.status == "grabbed":
            return ctx.error(409, "want is already grabbed")
    from hunter.slskd_client import SlskdClient, SlskdError

    try:
        with SlskdClient(cfg.slskd_url, cfg.slskd_api_key) as slskd:
            slskd.enqueue(username, files)
    except SlskdError as exc:
        return ctx.error(502, f"slskd: {exc}")
    with common.state(cfg) as st:
        st.mark_grabbed(want_id, username, dir_)
        st.record_peer_grab(username)
    log.info("manual grab via ui: want %d from %s", want_id, username)
    return ctx.json(200, {"grabbed": True})


@post("/api/resolve_url")
def resolve_url(ctx: Ctx):
    cfg = ctx.cfg
    url = ctx.b("url")
    m = re.search(r"musicbrainz\.org/release-group/([0-9a-f-]{36})", url)
    if m:
        try:
            rg = mb.mb_get(f"/release-group/{m.group(1)}", {"inc": "artist-credits"})
        except mb.MBUnavailable:
            return ctx.json(503, {"error": "MusicBrainz is busy", "retry": True})
        except (requests.RequestException, ValueError) as exc:
            return ctx.error(502, f"musicbrainz: {exc}")
        album = {
            "rgid": rg["id"],
            "album": rg.get("title", ""),
            "artist": mb.credit(rg.get("artist-credit")),
            "year": mb.rg_year(rg),
            "type": rg.get("primary-type") or "",
        }
        return ctx.json(
            200, {"kind": "album", "album": common.annotate_owned(cfg, [album])[0]}
        )
    m = re.search(r"musicbrainz\.org/artist/([0-9a-f-]{36})", url)
    if m:
        try:
            a = mb.mb_get(f"/artist/{m.group(1)}", {})
        except mb.MBUnavailable:
            return ctx.json(503, {"error": "MusicBrainz is busy", "retry": True})
        except (requests.RequestException, ValueError) as exc:
            return ctx.error(502, f"musicbrainz: {exc}")
        return ctx.json(
            200,
            {"kind": "artist", "artist": {"id": a["id"], "name": a.get("name", "")}},
        )
    m = re.search(
        r"open\.spotify\.com(?:/[a-z-]+)?/(album|track|playlist)/([A-Za-z0-9]+)", url
    )
    if m:
        sp_kind, sp_id = m.group(1), m.group(2)
        if sp_kind == "playlist":
            if cfg.spotify_configured:
                from hunter.spotify import (
                    SpotifyClient,
                    SpotifyError,
                    SpotifyRateLimitError,
                )

                try:
                    with SpotifyClient(
                        cfg.spotify_client_id,
                        cfg.spotify_client_secret,
                        cfg.token_file,
                        proxy=cfg.spotify_proxy,
                    ) as client:
                        pl = client.playlist_info(sp_id)
                    with common.state(cfg) as st:
                        st.upsert_playlist(pl.id, pl.name, pl.track_count)
                    return ctx.json(
                        200,
                        {
                            "kind": "playlist",
                            "detail": (
                                f"Playlist “{pl.name}” ({pl.track_count} tracks)"
                                " registered — flip Hunt/Mirror in Spotify → Playlists."
                            ),
                        },
                    )
                except SpotifyRateLimitError as exc:
                    return ctx.json(
                        503,
                        {"error": f"spotify rate limited: {exc}", "retry": True},
                    )
                except SpotifyError as exc:
                    return ctx.error(502, f"spotify: {exc}")
            return ctx.json(
                200,
                {
                    "kind": "playlist",
                    "detail": "Connect Spotify first, then paste this link again.",
                },
            )
        if not cfg.spotify_configured:
            return ctx.error(400, "connect Spotify to resolve Spotify links")
        from hunter.spotify import (
            SpotifyClient,
            SpotifyError,
            SpotifyRateLimitError,
        )

        try:
            with SpotifyClient(
                cfg.spotify_client_id,
                cfg.spotify_client_secret,
                cfg.token_file,
                proxy=cfg.spotify_proxy,
            ) as client:
                sp_album = (
                    client.album(sp_id)
                    if sp_kind == "album"
                    else client.track_album(sp_id)
                )
        except SpotifyRateLimitError as exc:
            return ctx.json(
                503, {"error": f"spotify rate limited: {exc}", "retry": True}
            )
        except SpotifyError as exc:
            return ctx.error(502, f"spotify: {exc}")
        try:
            hits = mb.search("album", f"{sp_album.artist} {sp_album.name}")
        except (requests.RequestException, ValueError, mb.MBUnavailable):
            hits = []
        match = next(
            (
                h
                for h in hits
                if norm_artist(h["artist"]) == norm_artist(sp_album.artist)
                and norm_album(h["album"]) == norm_album(sp_album.name)
            ),
            None,
        )
        if match:
            return ctx.json(
                200,
                {"kind": "album", "album": common.annotate_owned(cfg, [match])[0]},
            )
        have = common.already_have(
            cfg,
            sp_album.artist,
            sp_album.name,
            total=sp_album.total_tracks or None,
        )
        if have:
            return ctx.json(
                200,
                {
                    "kind": "owned",
                    "detail": (
                        f"Already in your library: {sp_album.artist} — {sp_album.name}"
                        f" ({have.item_count}/{have.expected} tracks). Nothing queued."
                    ),
                },
            )
        if blocklist.is_blocked(sp_album.artist, cfg):
            return ctx.error(
                409, f"artist {sp_album.artist!r} is on the import blocklist"
            )
        with common.state(cfg) as st:
            st.upsert_want(
                f"manual:{norm_artist(sp_album.artist)}:{norm_album(sp_album.name)}",
                sp_album.artist,
                sp_album.name,
                sp_album.year,
                sp_album.total_tracks or None,
                "manual",
                resurrect=True,
                cfg=cfg,
            )
            st.log_event(
                "request",
                sp_album.artist,
                sp_album.name,
                "spotify url, no MB match — free-text want",
            )
        common.spawn_pass()
        return ctx.json(
            200,
            {
                "kind": "queued",
                "detail": (
                    f"No MusicBrainz match for {sp_album.artist} — {sp_album.name};"
                    " queued as a free-text want instead."
                ),
            },
        )
    return ctx.error(
        400,
        "unrecognized URL — MusicBrainz release-group/artist and"
        " Spotify album/track/playlist links are supported",
    )

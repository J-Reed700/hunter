"""Spotify: cache-first library management, sync control, auth hookup, playlist mirror."""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path

import requests

from hunter import blocklist, config as config_mod, spotify as spotify_mod
from hunter.domain import norm_album, norm_artist
from hunter.web import common
from hunter.web.router import get, post
from hunter.web.server import Ctx

log = common.log
_SYNC_LOCK = threading.Lock()


def _want_key(album_id: str, artist: str, album: str) -> str:
    return album_id or f"manual:{norm_artist(artist)}:{norm_album(album)}"


@get("/api/spotify/overview")
def overview(ctx: Ctx):
    from hunter import spsync

    with common.state(ctx.cfg) as st:
        ov = spsync.overview(st, ctx.cfg)
        pls = st.playlists()
        ov["playlists"] = {
            "total": len(pls),
            "hunting": sum(1 for p in pls if p["hunt"]),
            "mirroring": sum(1 for p in pls if p["mirror"]),
        }
        ov["wants_from_spotify"] = st.spotify_want_counts()
    return ctx.json(200, ov)


@get("/api/spotify/playlists")
def playlists(ctx: Ctx):
    with common.state(ctx.cfg) as st:
        rows = [dict(r) for r in st.playlists()]
        per = st.playlist_want_counts()
    for r in rows:
        r["wants"] = per.get(r["name"], {})
    return ctx.json(200, {"playlists": rows})


@get("/api/spotify/playlist_tracks")
def playlist_tracks(ctx: Ctx):
    pid = ctx.q("id")
    if not pid:
        return ctx.error(400, "id required")
    with common.state(ctx.cfg) as st:
        meta = st.playlist_meta(pid)
        if not meta:
            return ctx.error(404, "unknown playlist")
        tracks = [dict(r) for r in st.sp_playlist_track_rows(pid)]
        wm = st.wants_status_map(
            [
                _want_key(t.get("album_id", ""), t["artist"], t["album"])
                for t in tracks
            ]
        )
    for t in tracks:
        k = _want_key(t.get("album_id", ""), t["artist"], t["album"])
        t["liked"] = bool(t.pop("liked_gen", 0))
        t["want_key"] = k
        t["want"] = wm.get(k)
    common.annotate_sp_owned(ctx.cfg, tracks)
    albums: dict[str, dict] = {}
    for t in tracks:
        k = t["want_key"]
        a = albums.setdefault(
            k,
            {
                "key": k,
                "want_key": k,
                "artist": t["artist"],
                "album": t["album"],
                "album_id": t.get("album_id", ""),
                "beets_album_id": t.get("beets_album_id"),
                "year": t.get("year"),
                "album_total": t.get("album_total"),
                "tracks": 0,
                "owned": t.get("owned", False),
                "have": t.get("have", 0),
                "expected": t.get("expected"),
                "want": t["want"],
            },
        )
        a["tracks"] += 1
    summary = {
        "tracks": len(tracks),
        "owned": sum(1 for t in tracks if t.get("owned")),
        "wanted": sum(
            1
            for t in tracks
            if (t["want"] or {}).get("status") in ("wanted", "grabbed")
        ),
        "albums": len(albums),
        "albums_owned": sum(1 for a in albums.values() if a.get("owned")),
        "albums_wanted": sum(
            1
            for a in albums.values()
            if (a["want"] or {}).get("status") in ("wanted", "grabbed")
        ),
    }
    return ctx.json(
        200,
        {
            "id": pid,
            "name": meta["name"],
            "track_count": meta["track_count"],
            "state": meta["tracks_state"],
            "synced_ts": meta["tracks_synced_ts"],
            "hunt": bool(meta["hunt"]),
            "mirror": bool(meta["mirror"]),
            "tracks": tracks,
            "albums": list(albums.values()),
            "summary": summary,
        },
    )


def _filter_rows(rows: list[dict], filt: str) -> list[dict]:
    if filt == "owned":
        return [r for r in rows if r.get("owned")]
    if filt == "missing":
        return [
            r
            for r in rows
            if not r.get("owned")
            and (r.get("want") or {}).get("status") not in ("wanted", "grabbed")
        ]
    if filt == "wanted":
        return [
            r
            for r in rows
            if (r.get("want") or {}).get("status") in ("wanted", "grabbed")
        ]
    if filt == "partial":
        return [r for r in rows if not r.get("owned") and (r.get("have") or 0) > 0]
    return rows


@get("/api/spotify/saved_albums")
def saved_albums(ctx: Ctx):
    q, sort = ctx.q("q"), ctx.q("sort", "name")
    offset = ctx.qi("offset", 0, 0)
    filt = ctx.q("filter")
    with common.state(ctx.cfg) as st:
        if filt:
            rows, total = st.sp_saved_album_rows(q, sort, 5000, 0)
        else:
            rows, total = st.sp_saved_album_rows(q, sort, 100, offset)
        albums = [dict(r) for r in rows]
        wm = st.wants_status_map([a["album_id"] for a in albums])
    common.annotate_sp_owned(ctx.cfg, albums, album_key="name")
    for a in albums:
        a["want_key"] = a["album_id"]
        a["want"] = wm.get(a["album_id"])
    if filt:
        albums = _filter_rows(albums, filt)
        total = len(albums)
        albums = albums[offset : offset + 100]
    return ctx.json(200, {"albums": albums, "total": total})


@get("/api/spotify/liked")
def liked(ctx: Ctx):
    q = ctx.q("q")
    offset = ctx.qi("offset", 0, 0)
    filt = ctx.q("filter")
    with common.state(ctx.cfg) as st:
        if filt:
            rows, total = st.sp_liked_rows(q, 20000, 0)
        else:
            rows, total = st.sp_liked_rows(q, 100, offset)
        tracks = [dict(r) for r in rows]
        wm = st.wants_status_map(
            [
                _want_key(t.get("album_id", ""), t["artist"], t["album"])
                for t in tracks
            ]
        )
    for t in tracks:
        k = _want_key(t.get("album_id", ""), t["artist"], t["album"])
        t["liked"] = True
        t["want_key"] = k
        t["want"] = wm.get(k)
    common.annotate_sp_owned(ctx.cfg, tracks)
    if filt:
        tracks = _filter_rows(tracks, filt)
        total = len(tracks)
        tracks = tracks[offset : offset + 100]
    return ctx.json(200, {"tracks": tracks, "total": total})


@get("/api/spotify/artists")
def sp_artists(ctx: Ctx):
    q = ctx.q("q")
    with common.state(ctx.cfg) as st:
        rows = st.sp_artist_rows(q)
        followed = {
            norm_artist(r["name"]): r["mbid"] for r in st.followed_artists()
        }
    lib = common.library(ctx.cfg)
    out = []
    for r in rows:
        n = norm_artist(r["name"])
        out.append(
            {
                "artist_id": r["artist_id"],
                "name": r["name"],
                "genres": r["genres"],
                "hunter_followed": n in followed,
                "hunter_mbid": followed.get(n, ""),
            }
        )
    if lib is not None:
        counts = lib.artist_album_counts()
        for o in out:
            o["library_albums"] = counts.get(norm_artist(o["name"]), 0)
    return ctx.json(200, {"artists": out})


@get("/api/spotify/album_tracks")
def album_tracks(ctx: Ctx):
    """Cached tracks of one Spotify album."""
    aid = ctx.q("id")
    with common.state(ctx.cfg) as st:
        rows = [dict(r) for r in st.sp_album_track_rows(aid)]
    for t in rows:
        t["liked"] = bool(t.pop("liked_gen", 0))
    return ctx.json(200, {"tracks": rows})


# ---- sync ----------------------------------------------------------------

@post("/api/spotify/sync")
def sync(ctx: Ctx):
    cfg = ctx.cfg
    if not cfg.spotify_configured:
        return ctx.error(400, "connect Spotify first")
    scope = ctx.b("scope") or "all"
    pid = ctx.b("id") or None
    if scope not in ("all", "playlists", "albums", "liked", "artists", "playlist"):
        return ctx.error(400, "bad scope")
    if scope == "playlist" and not pid:
        return ctx.error(400, "id required")

    from hunter import spsync

    ignore_cd = ctx.bb("ignore_cooldown")

    with _SYNC_LOCK:
        locked = spsync.is_sync_busy(cfg)
        if ctx.bb("reset") and locked:
            return ctx.error(409, "sync is currently running — wait before resetting")
        with common.state(cfg) as st:
            cool = spsync.cooldown_remaining(st)
            job = spsync.get_job(st)
            busy = locked or bool(
                job
                and job.get("status") == "running"
                and time.time() - job.get("updated_ts", 0) < 900
            )
            if ctx.bb("reset") and job:
                st.set_meta(spsync.JOB_META, "")
                busy = False
        if cool > 0 and not ignore_cd:
            return ctx.json(200, {"status": "cooldown", "cooldown_s": cool})
        if busy:
            return ctx.json(200, {"status": "running", "busy": True})

        def _work() -> None:
            try:
                spsync.run_sync_job(
                    cfg,
                    scope,
                    pid,
                    budget_s=840,
                    max_requests=80,
                    ignore_cooldown=ignore_cd,
                )
            except Exception as exc:                    # noqa: BLE001
                log.warning("ui-triggered sync run failed: %s", exc)

        threading.Thread(target=_work, daemon=True, name="sp-sync").start()
    return ctx.json(200, {"status": "started", "scope": scope})


@post("/api/spotify/request")
def request(ctx: Ctx):
    cfg = ctx.cfg
    items = ctx.body.get("items")
    if not isinstance(items, list) or not items or len(items) > 500:
        return ctx.error(400, "items: 1-500 required")
    force = ctx.bb("force")
    source = ctx.b("source") or "request"
    if not (
        source == "request"
        or source.startswith("playlist:")
        or source in ("saved_albums", "liked")
    ):
        source = "request"
    skipped_rows: list[dict] = []
    added = blocked = 0
    with common.state(cfg) as st:
        for it in items:
            if not isinstance(it, dict):
                continue
            artist = str(it.get("artist", "")).strip()
            album = str(it.get("album", "")).strip()
            if not artist or not album:
                continue
            if blocklist.is_blocked(artist, cfg):
                blocked += 1
                continue
            key = _want_key(str(it.get("album_id", "")).strip(), artist, album)
            try:
                total = max(0, int(it.get("album_total") or it.get("total_tracks") or 0)) or None
            except (TypeError, ValueError):
                total = None
            try:
                year = max(0, int(it.get("year") or 0))
            except (TypeError, ValueError):
                year = 0
            if not force:
                have = common.already_have(cfg, artist, album, total=total)
                if have:
                    skipped_rows.append(
                        {
                            "artist": artist,
                            "album": album,
                            "have": have.item_count,
                            "expected": have.expected,
                        }
                    )
                    continue
            if st.upsert_want(key, artist, album, year, total, source, resurrect=True, cfg=cfg):
                added += 1
                st.log_event("request", artist, album, "spotify pick via ui")
    if added:
        common.spawn_pass()
    log.info(
        "spotify pick: %d/%d album(s) requested, %d already owned",
        added,
        len(items),
        len(skipped_rows),
    )
    return ctx.json(
        200,
        {
            "added": added,
            "skipped_owned": len(skipped_rows),
            "skipped_blocked": blocked,
            "skipped": skipped_rows[:20],
        },
    )


@post("/api/spotify/drop")
def sp_drop(ctx: Ctx):
    """Un-want by Spotify keys (album ids / manual keys)."""
    keys = ctx.body.get("keys")
    if not isinstance(keys, list) or not keys:
        return ctx.error(400, "keys required")
    n = 0
    with common.state(ctx.cfg) as st:
        for k in keys[:500]:
            wid = st.want_id_by_key(str(k))
            if wid is not None:
                st.set_status(wid, "skipped", "dropped via spotify tab")
                n += 1
    return ctx.json(200, {"dropped": n})


@post("/api/playlist_toggle")
def playlist_toggle(ctx: Ctx):
    cfg = ctx.cfg
    pid, field, value = ctx.b("id"), ctx.b("field"), ctx.bb("value")
    if not pid or field not in ("hunt", "mirror"):
        return ctx.error(400, "id + field (hunt|mirror) required")
    with common.state(cfg) as st:
        name = st.set_playlist_flag(pid, field, value)
        if name is None:
            return ctx.error(404, "unknown playlist")
        moved = added = 0
        if field == "hunt":
            src = f"playlist:{name}"
            if value:
                moved = st.set_source_status(src, "skipped", "wanted")
                from hunter import spsync

                added = spsync.ingest_playlist_wants(cfg, st, pid, name)
                st.log_event(
                    "automation",
                    detail=(
                        f"hunt ON for playlist {name!r}: {added} new want(s),"
                        f" {moved} resurrected"
                    ),
                )
                common.spawn_pass()
            else:
                moved = st.set_source_status(src, "wanted", "skipped")
    log.info(
        "playlist %r %s=%s (%d wants moved, %d new)",
        name,
        field,
        value,
        moved,
        added,
    )
    return ctx.json(200, {"name": name, "moved": moved, "added": added})


@post("/api/spotify/playlist_forget")
def playlist_forget(ctx: Ctx):
    """Remove a pasted/foreign playlist from hunter's roster."""
    pid = ctx.b("id")
    with common.state(ctx.cfg) as st:
        meta = st.playlist_meta(pid)
        if not meta:
            return ctx.error(404, "unknown playlist")
        name = meta["name"]
        n = st.set_source_status(f"playlist:{name}", "wanted", "skipped")
        st.sp_delete_playlist(pid)
        st.log_event(
            "automation", detail=f"playlist {name!r} forgotten via ui ({n} wants parked)"
        )
    return ctx.json(200, {"forgotten": name, "parked": n})


# ---- auth ----------------------------------------------------------------

@get("/api/settings", role="operator")
def settings(ctx: Ctx):
    cfg = ctx.cfg
    last_sync = None
    try:
        hb = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
        if isinstance(hb, dict):
            last_sync = hb.get("sync")
    except (OSError, ValueError):
        pass
    has_spdc = False
    try:
        tok = json.loads(cfg.token_file.read_text(encoding="utf-8"))
        if isinstance(tok, dict):
            has_spdc = bool(tok.get("sp_dc"))
    except (OSError, ValueError):
        pass
    return ctx.json(
        200,
        {
            "spotify": {
                "creds_set": bool(cfg.spotify_client_id and cfg.spotify_client_secret),
                "connected": cfg.spotify_configured,
                "cookie_auth": has_spdc,
                "last_sync": last_sync,
                "proxy": bool(cfg.spotify_proxy),
            },
            "navidrome": {
                "configured": bool(cfg.navidrome_user),
                "url": cfg.navidrome_url,
            },
            "slskd": {"configured": cfg.slskd_configured, "url": cfg.slskd_url},
            "prowlarr": {"configured": cfg.prowlarr_configured},
            "audiomuse": {"configured": bool(cfg.audiomuse_token)},
            "webhook": bool(cfg.webhook_url),
            "ui_token": bool(cfg.ui_token),
            "console": cfg.beets_console_available,
            "paths": {
                "state": str(cfg.state_dir),
                "beets_db": str(cfg.beets_db),
                "automation": str(cfg.beets_automation_dir),
                "music_root": str(cfg.music_root),
                "quarantine": str(cfg.quarantine_dir),
            },
        },
    )


@post("/api/spotify/spdc", audit=False)
def spdc(ctx: Ctx):
    cfg = ctx.cfg
    sp_dc = ctx.b("sp_dc").strip('"')
    if not sp_dc:
        return ctx.error(400, "sp_dc required")
    from hunter.spotify import SpotifyClient, SpotifyError

    name = "?"
    with tempfile.TemporaryDirectory() as td:
        probe_file = Path(td) / "probe_token.json"
        config_mod.save_spotify_token(probe_file, {"sp_dc": sp_dc})
        try:
            with SpotifyClient("", "", probe_file, proxy=cfg.spotify_proxy) as probe:
                probe.get_access_token(force_refresh=True)
                try:
                    me = probe.me()
                    name = me.get("display_name") or me.get("id") or "?"
                except SpotifyError:
                    name = "(profile rate-limited — cookie is valid)"
        except SpotifyError as exc:
            return ctx.error(502, str(exc))

    config_mod.save_spotify_token(cfg, {"sp_dc": sp_dc})
    if ctx.user:
        with ctx.auth() as a:
            a.audit(ctx.user, "POST /api/spotify/spdc", f"user={name}", "", ctx.ip)
    log.info("spotify connected via sp_dc cookie (user %s)", name)
    common.spawn_pass()
    return ctx.json(200, {"connected": True, "user": name})


@post("/api/spotify/creds")
def creds(ctx: Ctx):
    cid, secret = ctx.b("client_id"), ctx.b("client_secret")
    if not cid or not secret:
        return ctx.error(400, "client_id and client_secret required")
    config_mod.save_spotify_app(ctx.cfg, cid, secret)
    return ctx.json(200, {"saved": True})


@get("/api/spotify/auth_url", role="operator")
def auth_url(ctx: Ctx):
    cfg = ctx.cfg
    if not cfg.spotify_client_id:
        return ctx.error(400, "save client creds first")
    return ctx.json(
        200, {"url": spotify_mod.oauth_authorize_url(cfg.spotify_client_id)}
    )


@post("/api/spotify/exchange", audit=False)
def exchange(ctx: Ctx):
    cfg = ctx.cfg
    code = spotify_mod.extract_oauth_code(ctx.b("url"))
    if not code:
        return ctx.error(400, "no ?code= in that URL")
    if not cfg.spotify_client_id or not cfg.spotify_client_secret:
        return ctx.error(400, "save client creds first")
    try:
        tok = spotify_mod.oauth_exchange_code(
            cfg.spotify_client_id,
            cfg.spotify_client_secret,
            code,
            cfg=cfg,
            proxy=cfg.spotify_proxy,
        )
    except spotify_mod.SpotifyError as exc:
        return ctx.error(502, f"spotify: {exc}")
    refresh_token = tok.get("refresh_token") if isinstance(tok, dict) else ""
    if not refresh_token:
        return ctx.error(502, "no refresh_token returned")
    config_mod.save_spotify_token(cfg, {"refresh_token": refresh_token})
    if ctx.user:
        with ctx.auth() as a:
            a.audit(ctx.user, "POST /api/spotify/exchange", "oauth", "", ctx.ip)
    common.spawn_pass()
    return ctx.json(200, {"connected": True})


@post("/api/spotify/disconnect")
def disconnect(ctx: Ctx):
    config_mod.delete_spotify_token(ctx.cfg)
    return ctx.json(200, {"connected": False})


# ---- Navidrome playlists / radio ------------------------------------------

@post("/api/make_playlist")
def make_playlist(ctx: Ctx):
    cfg = ctx.cfg
    name = ctx.b("name")[:80]
    raw_tracks = ctx.body.get("tracks")
    if not name or not isinstance(raw_tracks, list) or not raw_tracks or len(raw_tracks) > 300:
        return ctx.error(400, "name + 1-300 tracks required")
    if not cfg.navidrome_user:
        return ctx.error(400, "Navidrome creds not set")
    pairs = [
        (str(t.get("artist", "")).strip(), str(t.get("title", "")).strip())
        for t in raw_tracks
        if isinstance(t, dict) and str(t.get("title", "")).strip()
    ]
    from hunter.playlist_sync import create_navidrome_playlist

    res = create_navidrome_playlist(
        cfg, name, tracks=pairs, overwrite=ctx.bb("overwrite")
    )
    if not res.get("ok"):
        return ctx.error(int(res.get("status", 502)), res.get("error", "navidrome error"))
    return ctx.json(
        200,
        {
            "ok": True,
            "name": res["name"],
            "matched": res["matched"],
            "total": len(raw_tracks),
        },
    )


@post("/api/radio")
def radio(ctx: Ctx):
    cfg = ctx.cfg
    title, artist = ctx.b("title"), ctx.b("artist")
    if not title or not artist:
        return ctx.error(400, "title + artist required")
    if not cfg.audiomuse_token:
        return ctx.error(400, "AudioMuse token not set")
    if not cfg.navidrome_user:
        return ctx.error(400, "Navidrome creds not set")
    try:
        resp = requests.get(
            f"{cfg.audiomuse_url}/api/similar_tracks",
            params={"title": title, "artist": artist, "n": 40},
            headers={"Authorization": f"Bearer {cfg.audiomuse_token}"},
            timeout=25,
        )
        if resp.status_code != 200:
            return ctx.error(
                502,
                f"AudioMuse: {resp.status_code} — track may not be analyzed yet",
            )
        similar = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return ctx.error(502, f"AudioMuse: {exc}")
    ids = [
        str(s["item_id"])
        for s in (similar if isinstance(similar, list) else [])
        if isinstance(s, dict) and s.get("item_id")
    ]
    if not ids:
        return ctx.error(502, "no sonic neighbors yet — analysis still running")
    from hunter.playlist_sync import create_navidrome_playlist

    name = f"Radio · {title[:40]}"
    res = create_navidrome_playlist(cfg, name, song_ids=ids, overwrite=True)
    if not res.get("ok"):
        return ctx.error(int(res.get("status", 502)), res.get("error", "navidrome error"))
    return ctx.json(200, {"name": res["name"], "tracks": res["matched"]})

"""Incremental, resumable Spotify library sync.

Transport: the web player's own GraphQL API (spgql.py), authorized by the
sp_dc cookie's web token. One page per request, cursor persisted per page
inside batched SQLite transactions, and stale-gen sweeps run only on full
stage completion.
"""
from __future__ import annotations

import json
import logging
import re
import time

from hunter import library, spgql
from hunter.config import HunterConfig
from hunter.lock import LockBusy, hold, is_locked
from hunter.spotify import (
    SpAlbum,
    SpotifyClient,
    SpotifyError,
    SpotifyRateLimitError,
    SpPlaylist,
    SpTrack,
)
from hunter.state import HunterState

log = logging.getLogger(__name__)

JOB_META = "sp_sync_job"
COOLDOWN_META = "spotify_cooldown"          # shared with cli/pipeline
COMPLETED_META = "sp_sync_completed_ts"
COOLDOWN_SECONDS = 7200
RESYNC_AFTER_S = 86_400                     # a done job goes stale after 24h

_SCOPE_PLANS = {
    "all": ["playlists", "playlist_tracks", "saved_albums", "liked", "artists"],
    "playlists": ["playlists", "playlist_tracks"],
    "albums": ["saved_albums"],
    "liked": ["liked"],
    "artists": ["artists"],
    "playlist": ["one_playlist"],
}


def _have(
    cfg: HunterConfig,
    artist: str,
    album: str,
    total: int | None = None,
) -> bool:
    """Do we already own this? Fails OPEN — a duplicate want is cheap and
    reconcile clears it; a false 'owned' silently drops music."""
    try:
        lib = library.get_cached_library(cfg)
    except Exception as exc:                       # noqa: BLE001
        log.warning(
            "owned-oracle load failed (%s) — sync will queue wants without the owned gate",
            exc,
        )
        return False
    if lib is None:
        return False
    try:
        return (
            lib.already_have(
                artist,
                album,
                expected_tracks=total or None,
                min_coverage=cfg.owned_coverage,
            )
            is not None
        )
    except Exception as exc:                       # noqa: BLE001
        log.warning("owned gate failed for %s — %s: %s", artist, album, exc)
        return False


class _Budget(Exception):
    """Wall-clock or request budget spent — park the job, resume later."""


def _page_done(items: list, offset: int, total: int, what: str) -> bool:
    """True when a paginated collection is exhausted.

    An empty first page when ``total > 0`` is treated as a degraded response
    (raises ``SpotifyError`` so the cursor is kept and stale-gen sweeps do not
    wipe cached rows). When ``offset > 0`` and ``items == []``, Spotify
    over-reported ``totalCount`` — treat the stream as exhausted cleanly.
    """
    if total > 0 and offset >= total:
        return True
    if not items:
        if total == 0 and offset == 0:
            return True
        if offset > 0:
            log.info(
                "%s: empty page at offset %d of %d — treating stream as exhausted",
                what,
                offset,
                total,
            )
            return True
        raise SpotifyError(
            f"{what}: empty page at offset {offset} of {total} — refusing to"
            " treat a degraded response as complete (cursor kept)"
        )
    return False


class _Ctx:
    """One engine run: clients + job + budget, durable after every page."""

    def __init__(
        self,
        cfg: HunterConfig,
        state: HunterState,
        spotify: SpotifyClient,
        gql: spgql.SpotifyGQL,
        job: dict,
        deadline_mono: float,
        max_requests: int,
    ):
        self.cfg = cfg
        self.state = state
        self.sp = spotify
        self.gql = gql
        self.job = job
        self.deadline = deadline_mono
        self.max_requests = max_requests
        self.requests = 0

    def check(self) -> None:
        if time.monotonic() > self.deadline or self.requests >= self.max_requests:
            raise _Budget

    def paged(self, n: int = 1) -> None:
        self.requests += n
        self.job["requests"] = self.job.get("requests", 0) + n

    def save(self) -> None:
        self.job["pace"] = self.gql.pace
        _save(self.state, self.job)


# --- job persistence ---------------------------------------------------------


def get_job(state: HunterState) -> dict | None:
    raw = state.get_meta(JOB_META, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save(state: HunterState, job: dict) -> None:
    job["updated_ts"] = int(time.time())
    state.set_meta(JOB_META, json.dumps(job))


def is_sync_busy(cfg: HunterConfig) -> bool:
    """True while another process/thread holds ``sp_sync.lock``."""
    return is_locked(cfg.state_dir / "sp_sync.lock")


def ensure_job(
    state: HunterState,
    scope: str = "all",
    playlist_id: str | None = None,
) -> dict:
    """Return the resumable in-flight job if it matches ``(scope, playlist_id)``,
    or start a fresh job for the requested scope."""
    req_scope = scope if scope in _SCOPE_PLANS else "all"
    req_pid = playlist_id or ""
    job = get_job(state)
    if (
        job
        and job.get("status") in ("running", "partial", "error", "cooldown")
        and job.get("plan")
        and job.get("scope") == req_scope
        and (job.get("playlist_id") or "") == req_pid
    ):
        return job

    plan = list(_SCOPE_PLANS[req_scope])
    job = {
        "status": "running",
        "scope": req_scope,
        "playlist_id": req_pid,
        "gen": int(time.time() * 1000),
        "plan": plan,
        "cursor": {},
        "stages": (job or {}).get("stages", {}),
        "pace": 0,
        "requests": 0,
        "new_wants": 0,
        "detail": "",
        "started_ts": int(time.time()),
        "updated_ts": int(time.time()),
    }
    _save(state, job)
    return job


def needs_sync(state: HunterState) -> bool:
    """True when no full sync has ever completed or the last one is stale."""
    try:
        done_ts = float(state.get_meta(COMPLETED_META, "0"))
    except ValueError:
        return True
    return time.time() - done_ts > RESYNC_AFTER_S


def cooldown_remaining(state: HunterState) -> int:
    try:
        until = float(state.get_meta(COOLDOWN_META, "0"))
    except ValueError:
        return 0
    return max(0, int(until - time.time()))


# --- stage runners -----------------------------------------------------------


def _compile_exclude(pattern: str) -> re.Pattern[str] | None:
    if not pattern:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        log.warning("ignoring invalid HUNTER_PLAYLIST_EXCLUDE regex %r: %s", pattern, exc)
        return None


def _run_paged_stage(
    ctx: _Ctx,
    *,
    stage_key: str,
    label: str,
    fetch_page,
    apply_items,
    on_complete,
) -> None:
    """Shared cursor-resumable pagination skeleton with atomic final sweep."""
    job, state = ctx.job, ctx.state
    offset = int(job["cursor"].get("offset", 0))
    while True:
        ctx.check()
        items, total = fetch_page(offset)
        ctx.paged()
        with state.transaction():
            apply_items(items)
            offset += len(items)
            done = _page_done(items, offset, total, label)
            job["cursor"] = {} if done else {"offset": offset}
            if done:
                job["stages"][stage_key] = on_complete()
            ctx.save()
        if done:
            return


def _stage_playlists(ctx: _Ctx) -> None:
    job, state, cfg = ctx.job, ctx.state, ctx.cfg
    gen = job["gen"]
    exclude = _compile_exclude(cfg.playlist_exclude)
    known = {r["spotify_id"]: r["track_count"] for r in state.playlists()}

    def _apply(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            parsed = spgql.parse_library_playlist(item)
            if not parsed:
                continue
            pid, name, owner, count = parsed
            if exclude and exclude.search(name):
                continue
            state.upsert_playlist(pid, name, count or known.get(pid, 0))
            state.sp_mark_playlist(pid, gen, owner)

    def _done() -> dict:
        removed = state.sp_sweep_playlists(gen)
        return {
            "ts": int(time.time()),
            "count": state.sp_counts()["playlists"],
            "removed": removed,
        }

    _run_paged_stage(
        ctx,
        stage_key="playlists",
        label="playlist roster",
        fetch_page=lambda off: ctx.gql.library_page("Playlists", off),
        apply_items=_apply,
        on_complete=_done,
    )


def _sync_one_playlist(ctx: _Ctx, playlist_id: str, name: str) -> None:
    """Crawl one playlist's tracks page by page; cursor-safe at every page."""
    job, state = ctx.job, ctx.state
    pl = job["cursor"].get("pl") or {}
    if pl and pl.get("pid") not in (None, playlist_id):
        log.info(
            "cursor belonged to %s, now crawling %s — restarting it",
            pl.get("pid"),
            playlist_id,
        )
        pl = {}
    gen = pl.get("gen") or int(time.time() * 1000)
    offset = int(pl.get("offset", 0))
    pos = int(pl.get("pos", 0))
    total = 0

    ctx.check()
    state.sp_playlist_begin(playlist_id)
    album_artists: dict[str, str] = {}
    while True:
        ctx.check()
        items, total = ctx.gql.playlist_page(playlist_id, offset)
        ctx.paged()
        with state.transaction():
            for item in items:
                if not isinstance(item, dict):
                    continue
                t = spgql.parse_track(item.get("itemV2"))
                if not t:
                    continue
                aa = t.get("album_artist") or t["artist"]
                if t.get("album_id"):
                    album_artists.setdefault(t["album_id"], aa)
                state.sp_upsert_track(
                    track_id=t["track_id"],
                    title=t["title"],
                    artist=t["artist"],
                    album=t["album"],
                    album_artist=aa,
                    album_id=t["album_id"],
                    year=t["year"],
                    duration_s=t["duration_s"],
                    album_total=t.get("album_total", 0),
                )
                state.sp_link_track(playlist_id, t["track_id"], pos, gen)
                pos += 1
            offset += len(items)
            done = _page_done(items, offset, total, f"playlist {playlist_id}")
            if done:
                state.sp_playlist_done(playlist_id, gen, total or pos)
                job["cursor"].pop("pl", None)
            else:
                job["cursor"]["pl"] = {
                    "offset": offset, "gen": gen, "pos": pos, "pid": playlist_id
                }
            ctx.save()
        if done:
            break
    _ingest_playlist_wants(ctx, playlist_id, name, album_artists=album_artists)


def ingest_playlist_wants(
    cfg: HunterConfig,
    state: HunterState,
    playlist_id: str,
    name: str,
    *,
    album_artists: dict[str, str] | None = None,
) -> int:
    """A hunt-enabled playlist's albums join the want queue from local cache."""
    if "playlists" not in cfg.want_sources:
        return 0
    if not state.playlist_flags().get(playlist_id, (False, True))[0]:
        return 0
    # Warm the beets library snapshot BEFORE opening a state.db write transaction.
    library.get_cached_library(cfg)
    added = skipped = 0
    aa_map = album_artists or {}
    with state.transaction():
        for r in state.sp_playlist_album_rows(playlist_id):
            artist = aa_map.get(r["album_id"]) or r["artist"]
            if _have(cfg, artist, r["album"], r["album_total"] or None):
                skipped += 1
                continue
            added += state.upsert_want(
                r["album_id"],
                artist,
                r["album"],
                r["year"],
                r["album_total"] or None,
                f"playlist:{name}",
                cfg=cfg,
            )
    if skipped:
        log.info(
            "playlist %r: %d new want(s), %d album(s) already owned",
            name,
            added,
            skipped,
        )
    return added


def _ingest_playlist_wants(
    ctx: _Ctx,
    playlist_id: str,
    name: str,
    *,
    album_artists: dict[str, str] | None = None,
) -> None:
    ctx.job["new_wants"] += ingest_playlist_wants(
        ctx.cfg, ctx.state, playlist_id, name, album_artists=album_artists
    )


def _stage_playlist_tracks(ctx: _Ctx) -> None:
    job, state = ctx.job, ctx.state
    roster = state.sp_roster_for_sync()
    done_ids = set(job["cursor"].get("done_ids") or [])
    for row in roster:
        pid = row["spotify_id"]
        if pid in done_ids:
            continue
        ctx.check()
        _sync_one_playlist(ctx, pid, row["name"])
        done_ids.add(pid)
        job["cursor"]["done_ids"] = sorted(done_ids)
        ctx.save()
    job["stages"]["playlist_tracks"] = {
        "ts": int(time.time()),
        "count": len(roster),
    }


def _stage_one_playlist(ctx: _Ctx) -> None:
    job = ctx.job
    pid = job.get("playlist_id") or ""
    row = ctx.state.playlist_meta(pid)
    if not row:
        job["detail"] = f"playlist {pid} not in roster — sync playlists first"
        return
    _sync_one_playlist(ctx, pid, row["name"])
    job["stages"]["one_playlist"] = {"ts": int(time.time()), "id": pid}


def _stage_saved_albums(ctx: _Ctx) -> None:
    job, state, cfg = ctx.job, ctx.state, ctx.cfg
    gen = job["gen"]
    want_saved = "saved_albums" in cfg.want_sources
    if want_saved:
        # Warm beets snapshot outside state.db write transaction.
        library.get_cached_library(cfg)

    def _apply(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            parsed = spgql.parse_library_album(item)
            if not parsed:
                continue
            aid, artist, name, year = parsed
            state.sp_upsert_saved_album(aid, artist, name, year, 0, gen)
            if want_saved and name and not _have(cfg, artist, name):
                job["new_wants"] += state.upsert_want(
                    aid, artist, name, year, None, "saved_albums", cfg=cfg
                )

    def _done() -> dict:
        removed = state.sp_sweep_saved_albums(gen)
        return {
            "ts": int(time.time()),
            "count": state.sp_counts()["saved_albums"],
            "removed": removed,
        }

    _run_paged_stage(
        ctx,
        stage_key="saved_albums",
        label="saved albums",
        fetch_page=lambda off: ctx.gql.library_page("Albums", off),
        apply_items=_apply,
        on_complete=_done,
    )


def _stage_liked(ctx: _Ctx) -> None:
    job, state = ctx.job, ctx.state
    gen = job["gen"]

    def _apply(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            t = spgql.parse_track(item.get("track"))
            if not t:
                continue
            state.sp_upsert_track(
                track_id=t["track_id"],
                title=t["title"],
                artist=t["artist"],
                album=t["album"],
                album_artist=t.get("album_artist") or t["artist"],
                album_id=t["album_id"],
                year=t["year"],
                duration_s=t["duration_s"],
                album_total=t.get("album_total", 0),
                liked_gen=gen,
            )

    def _done() -> dict:
        state.sp_sweep_liked(gen)
        return {
            "ts": int(time.time()),
            "count": state.sp_counts()["liked"],
        }

    _run_paged_stage(
        ctx,
        stage_key="liked",
        label="liked songs",
        fetch_page=lambda off: ctx.gql.liked_page(off),
        apply_items=_apply,
        on_complete=_done,
    )


def _stage_artists(ctx: _Ctx) -> None:
    job, state = ctx.job, ctx.state
    gen = job["gen"]

    def _apply(items: list) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            parsed = spgql.parse_library_artist(item)
            if not parsed:
                continue
            arid, name = parsed
            state.sp_upsert_artist(arid, name, "", gen)

    def _done() -> dict:
        removed = state.sp_sweep_artists(gen)
        return {
            "ts": int(time.time()),
            "count": state.sp_counts()["artists"],
            "removed": removed,
        }

    _run_paged_stage(
        ctx,
        stage_key="artists",
        label="followed artists",
        fetch_page=lambda off: ctx.gql.library_page("Artists", off),
        apply_items=_apply,
        on_complete=_done,
    )


_STAGES = {
    "playlists": _stage_playlists,
    "playlist_tracks": _stage_playlist_tracks,
    "one_playlist": _stage_one_playlist,
    "saved_albums": _stage_saved_albums,
    "liked": _stage_liked,
    "artists": _stage_artists,
}


# --- engine ------------------------------------------------------------------


def continue_sync(
    cfg: HunterConfig,
    state: HunterState,
    spotify: SpotifyClient,
    job: dict,
    budget_s: float,
    max_requests: int,
    *,
    ignore_cooldown: bool = False,
) -> dict:
    """Run the job until it finishes or the budget does. Never raises on
    Spotify trouble — the job doc carries the outcome."""
    if not ignore_cooldown and cooldown_remaining(state) > 0:
        job["status"] = "cooldown"
        _save(state, job)
        return job

    if not spotify.has_sp_dc():
        job["status"] = "error"
        job["detail"] = (
            "Spotify sync requires an sp_dc browser cookie "
            "(classic OAuth tokens cannot query web-player GraphQL)"
        )
        _save(state, job)
        return job

    job["status"] = "running"
    job["detail"] = ""
    with spgql.SpotifyGQL(spotify, cfg.state_dir) as gql:
        try:
            learned = float(job.get("pace") or 0)
        except (TypeError, ValueError):
            learned = 0.0
        if learned > gql.pace:
            gql.pace = min(learned, 90.0)
        _save(state, job)
        ctx = _Ctx(
            cfg,
            state,
            spotify,
            gql,
            job,
            time.monotonic() + float(budget_s),
            max_requests,
        )
        try:
            while job["plan"]:
                stage = job["plan"][0]
                _STAGES[stage](ctx)
                job["plan"].pop(0)
                job["cursor"] = {}
                ctx.save()
            job["status"] = "done"
            state.set_meta(COOLDOWN_META, "0")
            if job["scope"] == "all":
                state.set_meta(COMPLETED_META, str(int(time.time())))
            log.info(
                "spotify sync done: %d requests, %d new wants, stages %s",
                job["requests"],
                job["new_wants"],
                {k: v.get("count") for k, v in job["stages"].items()},
            )
        except _Budget:
            job["status"] = "partial"
            log.info(
                "spotify sync parked (budget spent): next=%s after %d reqs",
                job["plan"][0] if job["plan"] else "?",
                job["requests"],
            )
        except SpotifyRateLimitError as exc:
            job["detail"] = str(exc)[:300]
            job["pace"] = gql.pace
            job["status"] = "cooldown"
            delay = exc.retry_after if (exc.retry_after and exc.retry_after > COOLDOWN_SECONDS) else COOLDOWN_SECONDS
            state.set_meta(COOLDOWN_META, str(time.time() + delay))
            log.warning(
                "spotify sync rate-limited — cooldown so throttle de-escalates "
                "(cursor saved at %s)",
                job["plan"][0] if job["plan"] else "?",
            )
        except SpotifyError as exc:
            job["detail"] = str(exc)[:300]
            job["pace"] = gql.pace
            job["status"] = "error"
            log.error("spotify sync failed: %s", exc)
        except Exception as exc:                       # noqa: BLE001
            job["detail"] = f"unexpected error: {exc}"[:300]
            job["pace"] = gql.pace
            job["status"] = "error"
            log.exception("spotify sync crashed unexpectedly")
    _save(state, job)
    return job


def run_sync_job(
    cfg: HunterConfig,
    scope: str = "all",
    playlist_id: str | None = None,
    budget_s: float = 840,
    max_requests: int = 80,
    *,
    ignore_cooldown: bool = False,
) -> dict:
    """One locked engine run — entry point for scheduled pass and UI Sync."""
    with HunterState(cfg.state_db) as state:
        if not cfg.spotify_configured:
            return {
                "status": "error",
                "detail": "Spotify not configured — add sp_dc cookie in Settings",
                "requests": 0,
            }
        if not ignore_cooldown and cooldown_remaining(state) > 0:
            job = get_job(state) or {"status": "cooldown", "requests": 0}
            job["status"] = "cooldown"
            return job
        try:
            with hold(cfg.state_dir / "sp_sync.lock"):
                with SpotifyClient(
                    cfg.spotify_client_id,
                    cfg.spotify_client_secret,
                    cfg.token_file,
                    proxy=cfg.spotify_proxy,
                ) as spotify:
                    job = ensure_job(state, scope, playlist_id)
                    return continue_sync(
                        cfg,
                        state,
                        spotify,
                        job,
                        budget_s,
                        max_requests,
                        ignore_cooldown=ignore_cooldown,
                    )
        except LockBusy:
            job = get_job(state) or {"status": "running", "requests": 0}
            job["status"] = "running"
            return job


def job_summary(job: dict | None) -> dict:
    """Compact pass-summary/heartbeat shape."""
    if not job:
        return {"status": "never"}
    out: dict = {
        "status": job.get("status", "?"),
        "requests": job.get("requests", 0),
        "new": job.get("new_wants", 0),
    }
    if job.get("plan"):
        out["next_stage"] = job["plan"][0]
    if job.get("status") == "error":
        out["error"] = job.get("detail") or "error"
    if job.get("status") == "cooldown":
        out["cooldown"] = True
    return out


def overview(state: HunterState, cfg: HunterConfig) -> dict:
    """Everything the Spotify tab needs in one read — cache-only, instant."""
    job = get_job(state)
    return {
        "connected": cfg.spotify_configured,
        "busy": is_sync_busy(cfg),
        "cooldown_s": cooldown_remaining(state),
        "stale": needs_sync(state),
        "counts": state.sp_counts(),
        "job": job,
    }


# --- cache-backed Spotify for the mirror -------------------------------------


class CachedSpotify:
    """The slice of SpotifyClient that playlist_sync.mirror_playlists uses,
    served entirely from the sync cache via public HunterState methods."""

    def __init__(self, state: HunterState):
        self._state = state

    def playlists(self) -> list[SpPlaylist]:
        return [
            SpPlaylist(
                id=r["spotify_id"],
                name=r["name"],
                track_count=r["track_count"],
            )
            for r in self._state.sp_cached_playlists()
        ]

    def playlist_tracks(self, playlist_id: str) -> list[SpTrack]:
        return [
            SpTrack(
                id=r["track_id"],
                title=r["title"],
                artist=r["artist"],
                duration_s=r["duration_s"],
                album=SpAlbum(
                    id=r["album_id"],
                    artist=r["album_artist"] or r["artist"],
                    name=r["album"],
                    year=r["year"],
                    total_tracks=r["album_total"],
                ),
            )
            for r in self._state.sp_playlist_track_rows(playlist_id)
        ]

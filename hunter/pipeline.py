"""Hunter pass orchestration service — pure domain pipeline (zero argparse).

Callable by both the CLI (`hunter pass` / `hunter seed-*`) and Web API routes
without circular imports or private CLI reach-ins.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import ExitStack
from datetime import datetime
from typing import Any

from hunter import db, domain, libquery, storage
from hunter.config import HunterConfig
from hunter.library import BeetsLibrary
from hunter.lock import LockBusy, hold
from hunter.notify import Notifier
from hunter.prowlarr import ProwlarrClient, ProwlarrError, is_prowlarr_key
from hunter.prowlarr import rank as rank_releases
from hunter.slskd_client import SlskdClient, SlskdError, norm_dir, rank_candidates
from hunter.state import HunterState, Want

log = logging.getLogger("hunter.pipeline")

FAILURE_ALERT_AFTER = 3


def _date_minus_days(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86_400))


def _backoff(cfg: HunterConfig, attempts: int) -> int:
    idx = min(max(attempts - 1, 0), len(cfg.backoff_seconds) - 1)
    return cfg.backoff_seconds[idx]


def search_texts_for_want(want: Want) -> list[tuple[str, bool]]:
    """Tiered ``(query, includes_artist)`` fallbacks, tightest-to-loosest."""
    clean_album = domain.norm_album(want.album)
    tiers = [
        (f"{want.artist} {want.album}", True),
        (f"{domain.norm_artist(want.artist)} {clean_album}", True),
        (clean_album, False),
    ]
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    for text, has_artist in tiers:
        cleaned = " ".join(text.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            out.append((cleaned, has_artist))
    return out


# --------------------------------------------------------------------------
# Library queries & seeding
# --------------------------------------------------------------------------


def gap_rows(
    cfg: HunterConfig,
    coverage: float | None = None,
    min_missing: int = 2,
    min_expected: int = 4,
    max_expected: int = 40,
) -> list[tuple[int, str, str, int, int, int]]:
    """Partial albums in beets: ``(id, artist, album, year, have, expected)``."""
    return libquery.gap_rows(
        cfg,
        coverage=coverage,
        min_missing=min_missing,
        min_expected=min_expected,
        max_expected=max_expected,
    )


def lossy_rows(
    cfg: HunterConfig,
    min_tracks: int = 4,
) -> list[tuple[int, str, str, int, int]]:
    """Zero-lossless albums in beets: ``(id, artist, album, year, n)``."""
    return libquery.lossy_rows(cfg, min_tracks=min_tracks)


def seed_gaps(
    cfg: HunterConfig,
    state: HunterState,
    *,
    coverage: float | None = None,
    min_missing: int = 2,
    min_expected: int = 4,
    max_expected: int = 40,
    limit: int = 0,
    apply: bool = True,
    rows: list[tuple[int, str, str, int, int, int]] | None = None,
) -> int:
    if rows is None:
        rows = gap_rows(cfg, coverage, min_missing, min_expected, max_expected)
    if limit > 0:
        rows = rows[:limit]
    if not apply:
        return len(rows)
    new = 0
    with state.transaction():
        for album_id, artist, album, year, _have, expected in rows:
            new += int(
                state.upsert_want(
                    f"gapfill:beets:{album_id}",
                    artist,
                    album,
                    year or 0,
                    int(expected),
                    "gapfill",
                    cfg=cfg,
                )
            )
    return new


def seed_upgrades(
    cfg: HunterConfig,
    state: HunterState,
    *,
    min_tracks: int = 4,
    limit: int = 0,
    apply: bool = True,
    rows: list[tuple[int, str, str, int, int]] | None = None,
) -> int:
    if rows is None:
        rows = lossy_rows(cfg, min_tracks)
    if limit > 0:
        rows = rows[:limit]
    if not apply:
        return len(rows)
    new = 0
    with state.transaction():
        for album_id, artist, album, year, n in rows:
            new += int(
                state.upsert_want(
                    f"upgrade:beets:{album_id}",
                    artist,
                    album,
                    year or 0,
                    n,
                    "upgrade",
                    cfg=cfg,
                )
            )
    return new


def auto_reseed(cfg: HunterConfig, state: HunterState) -> dict[str, int] | None:
    """Weekly maintenance re-scan of beets for active gapfill/upgrade programs."""
    try:
        last = float(state.get_meta("last_reseed", "0"))
    except ValueError:
        last = 0.0
    if time.time() - last < 7 * 86_400:
        return None
    out: dict[str, int] = {}
    if state.source_active("gapfill"):
        out["gapfill"] = seed_gaps(cfg, state, coverage=cfg.owned_coverage, apply=True)
    if state.source_active("upgrade"):
        out["upgrade"] = seed_upgrades(cfg, state, apply=True)
    state.set_meta("last_reseed", str(time.time()))
    if any(out.values()):
        log.info("auto-reseed: %s", out)
        state.log_event("reseed", detail=json.dumps(out))
    return out


def apply_play_weights(cfg: HunterConfig, state: HunterState) -> int:
    """Weight active backlog wants (gapfill/upgrade/saved/saved_albums) by
    Navidrome play count within ``-40..0``.

    Sums duplicate ``(artist, album)`` rows and never overwrites operator or
    urgent priorities (``priority <= PRIORITY_OPERATOR``) or non-backlog sources.
    """
    if not cfg.navidrome_db.exists():
        return 0
    plays: dict[tuple[str, str], int] = {}
    nd: sqlite3.Connection | None = None
    try:
        nd = db.open_readonly_db(cfg.navidrome_db)
        for name, artist, n in nd.execute(
            "SELECT al.name, al.album_artist, an.play_count"
            " FROM album al JOIN annotation an"
            " ON an.item_id=al.id AND an.item_type='album'"
            " WHERE an.play_count > 0"
        ):
            key = (domain.norm_artist(artist or ""), domain.norm_album(name or ""))
            if key[0] and key[1]:
                plays[key] = plays.get(key, 0) + int(n or 0)
    except (sqlite3.Error, OSError) as exc:
        log.debug("play-weight read failed: %s", exc)
        return 0
    finally:
        if nd is not None:
            nd.close()
    if not plays:
        return 0

    pairs: list[tuple[int, int]] = []
    backlog_sources = ("gapfill", "upgrade", "saved", "saved_albums")
    for want in state.all_open():
        if want.source not in backlog_sources or want.priority <= domain.PRIORITY_OPERATOR:
            continue
        n = plays.get((domain.norm_artist(want.artist), domain.norm_album(want.album)), 0)
        prio = -min(n, 40)
        if prio != want.priority:
            pairs.append((want.id, prio))
    if not pairs:
        return 0
    return state.bulk_set_priority(pairs)


# --------------------------------------------------------------------------
# Pass phases
# --------------------------------------------------------------------------


def _quarantine_index(cfg: HunterConfig) -> dict[str, str]:
    """Walk ``cfg.quarantine_dir`` lazily only when a stale grab needs attribution."""
    roots = (
        "music",
        "duplicates",
        "not_wanted",
        "blocked_artist",
        "recovery",
        "superseded",
        "library_prune",
    )
    max_dirs = 60
    max_entries = 200_000
    datish = re.compile(r"\d{4}-?\d{2}-?\d{2}")
    out: dict[str, str] = {}

    def record(name: str, label: str) -> bool:
        out.setdefault(name, label)
        return len(out) < max_entries

    for cat in roots:
        root = cfg.quarantine_dir / cat
        try:
            if not root.is_dir():
                continue
        except OSError as exc:
            log.warning("quarantine root %s unreadable (%s) — continuing", root, exc)
            continue
        try:
            kids = [d for d in root.iterdir() if d.is_dir()]
            kids.sort(key=lambda d: (bool(datish.match(d.name)), d.name), reverse=True)
            for date_dir in kids[:max_dirs]:
                stamp = date_dir.name
                for entry in date_dir.iterdir():
                    if not entry.is_dir():
                        if not record(entry.name, f"{cat} ({stamp})"):
                            return out
                        continue
                    nested = False
                    if cat == "music":
                        try:
                            for leaf in entry.iterdir():
                                nested = True
                                if not record(leaf.name, f"{cat}/{entry.name} ({stamp})"):
                                    return out
                        except OSError:
                            pass
                    if not nested:
                        if not record(entry.name, f"{cat} ({stamp})"):
                            return out
        except OSError as exc:
            log.debug("quarantine walk failed for %s: %s", cat, exc)
    log.info("quarantine index: %d entries across %d category roots", len(out), len(roots))
    return out


def _recovery_files_present(
    cfg: HunterConfig,
    album_id: int,
    expected_tracks: int | None,
) -> bool:
    """Verify every beets track for *album_id* exists on disk before allowing a
    ``source == 'recovery'`` want to be marked ``done``."""
    try:
        conn = db.open_readonly_db(cfg.beets_db)
    except (sqlite3.Error, OSError):
        return False
    try:
        rows = conn.execute(
            "SELECT path FROM items WHERE album_id = ?", (album_id,)
        ).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()

    if not rows:
        return False
    present = 0
    for r in rows:
        host_p = domain.container_to_host_path(cfg, r[0])
        if host_p is None:
            continue
        try:
            os.stat(host_p)
            present += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in (errno.ENOTCONN, errno.EIO, errno.EACCES):
                log.warning("recovery disk verify blocked (%s) on %s", exc, host_p)
                return False
            continue
    exp = expected_tracks or len(rows)
    if exp <= 0:
        return False
    # Partial-loss albums (e.g. 8/10 tracks present) still need recovery until all DB rows exist on disk.
    return present >= len(rows) and (present / exp) >= cfg.owned_coverage


def _is_fast_fail(dl: dict | None, expected_tracks: int | None, min_coverage: float) -> bool:
    if not dl or dl.get("active", 0) > 0:
        return False
    errored = int(dl.get("errored") or 0)
    if errored <= 0:
        return False
    succeeded = int(dl.get("succeeded") or 0)
    if succeeded == 0:
        return True
    exp = expected_tracks or int(dl.get("total") or 1)
    min_needed = max(1, int(exp * min_coverage))
    return succeeded < min_needed


def reconcile(
    cfg: HunterConfig,
    state: HunterState,
    lib: BeetsLibrary,
    slskd: SlskdClient | None,
    now: float | None = None,
    *,
    notifier: Notifier | None = None,
) -> dict[str, int]:
    """Check every open want against beets + slskd transfers; advance state."""
    stats = {
        "done": 0,
        "reverted": 0,
        "exhausted": 0,
        "failed_fast": 0,
        "quarantined": 0,
    }
    now_ts = time.time() if now is None else float(now)

    dl_states: dict[tuple[str, str], dict] | None = None
    if slskd is not None:
        try:
            dl_states = slskd.download_states()
        except SlskdError as exc:
            log.warning("transfer snapshot unavailable: %s", exc)

    qindex: dict[str, str] | None = None

    def get_qindex() -> dict[str, str]:
        nonlocal qindex
        if qindex is None:
            qindex = _quarantine_index(cfg)
        return qindex

    def abandon(want: Want) -> None:
        if slskd is None or not want.grabbed_username or not want.grabbed_dir:
            return
        if is_prowlarr_key(want.grabbed_username):
            return
        try:
            n = slskd.cancel_dir(want.grabbed_username, want.grabbed_dir)
            if n:
                log.info(
                    "cancelled %d orphaned transfer(s) from %s for %s — %s",
                    n,
                    want.grabbed_username,
                    want.artist,
                    want.album,
                )
        except SlskdError as exc:
            log.debug("orphan cancel failed: %s", exc)

    def fail_grab(want: Want, *, fast: bool, q_reason: str | None = None) -> None:
        if fast:
            state.log_event(
                "download_failed",
                want.artist,
                want.album,
                f"peer {want.grabbed_username}: transfer errored",
            )
            if want.grabbed_username:
                state.record_peer_outcome(want.grabbed_username, False)
        elif q_reason:
            stats["quarantined"] += 1
            state.log_event("import_failed", want.artist, want.album, q_reason)
        elif want.grabbed_username:
            state.record_peer_outcome(want.grabbed_username, False)

        abandon(want)
        if want.attempts >= cfg.max_attempts:
            why = (
                "last download failed outright"
                if fast
                else (f"quarantined by beets: {q_reason}" if q_reason else "never landed")
            )
            state.set_status(want.id, "exhausted", f"{want.attempts} attempts, {why}")
            stats["exhausted"] += 1
        elif fast:
            state.revert_to_wanted(want.id, 0)
            stats["failed_fast"] += 1
        else:
            state.revert_to_wanted(want.id, _backoff(cfg, want.attempts))
            stats["reverted"] += 1

    def reconcile_grab_or_misses(want: Want, miss_reason: str, *, log_unfindable: bool = False) -> None:
        if want.status == "grabbed" and dl_states is not None:
            dl = dl_states.get((want.grabbed_username or "", norm_dir(want.grabbed_dir or "")))
            if _is_fast_fail(dl, want.track_count, cfg.owned_coverage):
                fail_grab(want, fast=True)
                return
        if (
            want.status == "grabbed"
            and want.grabbed_ts
            and now_ts - want.grabbed_ts > cfg.grab_stale_seconds
        ):
            leaf = domain.dir_leaf(want.grabbed_dir or "")
            q_reason = get_qindex().get(leaf) if leaf else None
            fail_grab(want, fast=False, q_reason=q_reason)
            return
        if want.status == "wanted" and want.search_misses >= cfg.search_miss_cap:
            state.set_status(want.id, "exhausted", miss_reason)
            stats["exhausted"] += 1
            if log_unfindable:
                log.warning(
                    "UNFINDABLE %s — %s (%d misses)",
                    want.artist,
                    want.album,
                    want.search_misses,
                )

    for want in state.all_open():
        # --- Upgrade wants ---------------------------------------------------
        if want.source == "upgrade" and want.spotify_album_id.startswith("upgrade:beets:"):
            try:
                album_id = int(want.spotify_album_id.rsplit(":", 1)[1])
            except ValueError:
                state.set_status(want.id, "exhausted", "invalid upgrade album key")
                stats["exhausted"] += 1
                continue

            ac = lib.album_coverage(album_id, want.track_count, cfg.owned_coverage)
            if ac is None:
                state.set_status(
                    want.id,
                    "done",
                    f"beets album {album_id} removed or merged",
                )
                stats["done"] += 1
                continue

            lossless = lib.lossless_count(album_id)
            target = max(1, int((want.track_count or ac.expected or 1) * cfg.owned_coverage))
            if lossless >= target:
                state.set_status(want.id, "done", f"upgraded: {lossless} lossless tracks")
                if want.status == "grabbed" and want.grabbed_username:
                    state.record_peer_outcome(want.grabbed_username, True)
                stats["done"] += 1
                log.info("UPGRADED %s — %s (%d lossless tracks)", want.artist, want.album, lossless)
                continue

            reconcile_grab_or_misses(
                want, f"no lossless copy found in {want.search_misses} searches"
            )
            continue

        # --- Gap-fill wants --------------------------------------------------
        if want.source == "gapfill" and want.spotify_album_id.startswith("gapfill:beets:"):
            try:
                aid = int(want.spotify_album_id.rsplit(":", 1)[1])
            except ValueError:
                aid = None
            if aid is not None:
                ac = lib.album_coverage(aid, want.track_count, cfg.owned_coverage)
                if ac is None:
                    state.set_status(
                        want.id,
                        "done",
                        f"beets album {aid} removed or merged",
                    )
                    stats["done"] += 1
                    continue
                if ac.owned:
                    state.set_status(
                        want.id,
                        "done",
                        f"gap filled: {ac.item_count}/{ac.expected} ({ac.coverage:.0%})",
                    )
                    if want.status == "grabbed" and want.grabbed_username:
                        state.record_peer_outcome(want.grabbed_username, True)
                    stats["done"] += 1
                    log.info(
                        "DONE(gapfill) %s — %s (%d/%d, %.0f%%)",
                        want.artist,
                        want.album,
                        ac.item_count,
                        ac.expected,
                        ac.coverage * 100,
                    )
                    continue

        # --- Standard & recovery wants --------------------------------------
        rgid = domain.extract_rgid_from_key(want.spotify_album_id)
        result = lib.lookup_owned(
            want.artist,
            want.album,
            rgid=rgid,
            expected_tracks=want.track_count,
            min_coverage=cfg.owned_coverage,
        )
        if result.owned:
            disk_ok = True
            if want.source == "recovery" and result.album_id is not None:
                disk_ok = _recovery_files_present(cfg, result.album_id, want.track_count)
            if disk_ok:
                state.set_status(
                    want.id,
                    "done",
                    f"beets album {result.album_id} ({result.item_count} items)",
                )
                if want.status == "grabbed" and want.grabbed_username:
                    state.record_peer_outcome(want.grabbed_username, True)
                stats["done"] += 1
                log.info(
                    "DONE %s — %s (beets album %s, coverage %.0f%%)",
                    want.artist,
                    want.album,
                    result.album_id,
                    result.coverage * 100,
                )
                continue

        reconcile_grab_or_misses(
            want,
            f"unfindable: {want.search_misses} searches, no viable candidate",
            log_unfindable=True,
        )

    if stats["exhausted"] and notifier is not None:
        notifier.send(
            f"hunter: {stats['exhausted']} want(s) exhausted all attempts — "
            "`docker exec hunter hunter status` for the list",
            force=True,
            bucket="alert",
            tags=["hunter", "warning"],
        )
    return stats


def _prowlarr_fallback(
    cfg: HunterConfig,
    prowlarr: ProwlarrClient,
    state: HunterState,
    want: Want,
    apply: bool,
    tried: set[tuple[str, str]],
) -> str | None:
    """Ask Prowlarr indexers after repeated Soulseek misses.

    Returns ``"grabbed"``, ``"would_grab"``, or ``None``.
    """
    lossless_only = want.source == "upgrade"
    for text, has_artist in search_texts_for_want(want):
        if not has_artist:
            continue
        try:
            releases = prowlarr.search(text, cfg.search_timeout)
        except ProwlarrError as exc:
            log.warning("prowlarr search failed for %r: %s", text, exc)
            continue
        ranked = rank_releases(
            releases,
            want.artist,
            want.album,
            prefer_lossless=cfg.prefer_lossless,
            lossless_only=lossless_only,
            tried=tried,
        )
        if not ranked:
            continue
        best = ranked[0]
        if not apply:
            log.info(
                "[dry-run] prowlarr would grab %s — %s: %r via %s (%s, %d MB, lossless=%s)",
                want.artist,
                want.album,
                best.title[:70],
                best.indexer,
                best.protocol,
                best.size >> 20,
                best.lossless,
            )
            state.bump_backoff(want.id, max(cfg.interval * 2, 3600), count_attempt=False)
            return "would_grab"
        for rel in ranked[:2]:
            try:
                prowlarr.grab(rel)
            except ProwlarrError as exc:
                log.warning("prowlarr grab failed on %s: %s — trying next", rel.indexer, exc)
                state.note_tried(want.id, *rel.key)
                tried.add(rel.key)
                continue
            state.mark_grabbed(want.id, *rel.key)
            log.info(
                "GRABBED %s — %s via prowlarr/%s (%s, %d MB, lossless=%s): %s",
                want.artist,
                want.album,
                rel.indexer,
                rel.protocol,
                rel.size >> 20,
                rel.lossless,
                rel.title[:70],
            )
            return "grabbed"
    return None


def hunt(
    cfg: HunterConfig,
    state: HunterState,
    slskd: SlskdClient,
    prowlarr: ProwlarrClient | None,
    apply: bool,
    limit: int,
    deadline: float,
) -> dict[str, int]:
    """Search slskd (and Prowlarr fallback) until *limit* grabs or deadline."""
    stats = {
        "searched": 0,
        "grabbed": 0,
        "would_grab": 0,
        "no_candidate": 0,
        "prowlarr": 0,
        "prowlarr_would_grab": 0,
        "transport_error": 0,
        "enqueue_error": 0,
    }
    consecutive_transport_failures = 0
    max_transport_failures = 3

    for dead in state.peers_to_autoban(cfg.peer_ban_strikes):
        if state.ban_peer(dead, f"auto: {cfg.peer_ban_strikes} failed grabs, 0 completed"):
            state.log_event("automation", detail=f"auto-banned flaky peer {dead}")
    banned = state.banned_peers()
    reputation = state.peer_reputation()

    search_cap = max(limit * 3, 20)
    for want in state.eligible_for_hunt(search_cap):
        if time.time() > deadline:
            log.warning("pass deadline hit — stopping hunt early")
            break
        done_count = (
            stats["grabbed"] + stats["prowlarr"]
            if apply
            else stats["would_grab"] + stats["prowlarr_would_grab"]
        )
        if done_count >= limit:
            break

        stats["searched"] += 1
        tried = state.tried_candidates(want.id)
        artist_tokens = domain.extract_match_tokens(domain.norm_artist(want.artist))
        ranked: list = []
        transport_failed = False

        for text, has_artist in search_texts_for_want(want):
            try:
                responses = slskd.search(text, cfg.search_timeout)
            except SlskdError as exc:
                if exc.status is not None and 400 <= exc.status < 500:
                    log.warning("slskd rejected query %r (%s) — trying next tier", text, exc)
                    consecutive_transport_failures = 0
                    continue
                log.error("slskd search failed for %r: %s", text, exc)
                transport_failed = True
                break

            consecutive_transport_failures = 0
            ranked = rank_candidates(
                responses,
                want.track_count,
                cfg.min_candidate_ratio,
                cfg.max_candidate_ratio,
                cfg.prefer_lossless,
                tried,
                min_bitrate=cfg.min_bitrate,
                banned=banned,
                reputation=reputation,
            )
            if not has_artist:
                if not artist_tokens:
                    ranked = []
                else:
                    ranked = [
                        c
                        for c in ranked
                        if domain.tokens_match_path(
                            artist_tokens, domain.normalize(c.dir), require_all=False
                        )
                    ]
            if want.source == "upgrade":
                ranked = [c for c in ranked if c.lossless]
            if ranked:
                best = ranked[0]
                log.info(
                    "%d candidate(s) for %s — %s via %r; best: %s:%s "
                    "(%d files, lossless=%s, ~%dkbps, slot=%s, queue=%d)",
                    len(ranked),
                    want.artist,
                    want.album,
                    text,
                    best.username,
                    domain.dir_leaf(best.dir),
                    best.audio_count,
                    best.lossless,
                    best.avg_bitrate,
                    best.has_free_slot,
                    best.queue_length,
                )
                break

        if transport_failed and not ranked:
            stats["transport_error"] += 1
            consecutive_transport_failures += 1
            log.warning(
                "slskd transport error for %s — %s: not counted as a miss",
                want.artist,
                want.album,
            )
            if consecutive_transport_failures >= max_transport_failures:
                log.error(
                    "slskd unreachable %d wants in a row — aborting hunt phase",
                    consecutive_transport_failures,
                )
                break
            continue

        if (
            not ranked
            and prowlarr is not None
            and want.search_misses + 1 >= cfg.prowlarr_after_misses
        ):
            pw = _prowlarr_fallback(cfg, prowlarr, state, want, apply, tried)
            if pw == "grabbed":
                stats["prowlarr"] += 1
                continue
            if pw == "would_grab":
                stats["prowlarr_would_grab"] += 1
                continue

        if not ranked:
            state.bump_backoff(
                want.id,
                _backoff(cfg, want.search_misses + 1),
                count_attempt=apply,
            )
            stats["no_candidate"] += 1
            log.info(
                "no candidate for %s — %s (miss %d, backing off)",
                want.artist,
                want.album,
                want.search_misses + 1,
            )
            continue

        if not apply:
            best = ranked[0]
            log.info(
                "[dry-run] would grab %s — %s from %s (%d files)",
                want.artist,
                want.album,
                best.username,
                best.audio_count,
            )
            state.bump_backoff(want.id, max(cfg.interval * 2, 3600), count_attempt=False)
            stats["would_grab"] += 1
            continue

        for cand in ranked[:3]:
            try:
                slskd.enqueue(cand.username, list(cand.files))
            except (SlskdError, KeyError) as exc:
                log.warning(
                    "enqueue failed %s/%s: %s — trying next candidate",
                    cand.username,
                    cand.dir,
                    exc,
                )
                state.note_tried(want.id, cand.username, cand.dir)
                tried.add((cand.username, cand.dir))
                continue
            state.mark_grabbed(want.id, cand.username, cand.dir)
            state.record_peer_grab(cand.username)
            stats["grabbed"] += 1
            log.info(
                "GRABBED %s — %s from %s (%d files, ~%dkbps, leaf=%s)",
                want.artist,
                want.album,
                cand.username,
                cand.audio_count,
                cand.avg_bitrate,
                domain.dir_leaf(cand.dir),
            )
            break
        else:
            # Peer/enqueue failures must not push the want toward "unfindable" exhaustion.
            state.bump_backoff(
                want.id,
                _backoff(cfg, want.attempts + 1),
                count_attempt=False,
            )
            stats["enqueue_error"] += 1

    return stats


def _mb_release_after_follow(
    release_date: str,
    followed_date: str,
    today: str,
) -> tuple[bool, bool]:
    """Return ``(in_window, is_fresh_drop)`` honoring MB date precision."""
    d = (release_date or "").strip()
    if not d:
        return False, False
    if len(d) == 4 and d.isdigit():
        in_win = (d > followed_date[:4] or (d == followed_date[:4] and followed_date[5:10] == "01-01")) and d <= today[:4]
        return in_win, False
    if len(d) == 7:
        in_win = (d > followed_date[:7] or (d == followed_date[:7] and followed_date[8:10] == "01")) and d <= today[:7]
        return in_win, False
    d10 = d[:10]
    in_win = followed_date <= d10 <= today
    fresh = in_win and d10 >= _date_minus_days(3)
    return in_win, fresh


def watch_artists(
    cfg: HunterConfig,
    state: HunterState,
    lib: BeetsLibrary | None,
    deadline: float,
) -> dict[str, int]:
    """Followed-artist new-release monitoring."""
    from hunter import mb

    stats = {"checked": 0, "new_wants": 0}
    due = state.artists_due_check(
        int(time.time()) - cfg.artist_check_hours * 3600,
        limit=20,
    )
    for artist in due:
        if time.time() > deadline:
            break
        try:
            rgs = mb.artist_release_groups(artist["mbid"])
        except Exception as exc:                       # noqa: BLE001
            log.warning("artist check failed for %s: %s", artist["name"], exc)
            continue

        followed_date = time.strftime("%Y-%m-%d", time.localtime(artist["followed_ts"]))
        today = time.strftime("%Y-%m-%d")
        keys = artist.keys() if hasattr(artist, "keys") else []
        want_types = {
            t.strip().lower()
            for t in (artist["want_types"] if "want_types" in keys else "album,ep").split(",")
            if t.strip()
        }

        hit_deadline = False
        for rg in rgs:
            if time.time() > deadline:
                hit_deadline = True
                break
            if not rg.get("date"):
                continue
            rtype = (rg.get("type") or "album").lower()
            secondary = rg.get("secondary") or []

            if not secondary and rg["date"] >= _date_minus_days(90):
                state.upsert_calendar(
                    rg["rgid"],
                    artist["mbid"],
                    artist["name"],
                    rg["album"],
                    rg["date"],
                    rtype,
                )

            if rtype not in want_types:
                continue
            if secondary:
                if "live" not in want_types:
                    continue
                if not all(str(s).lower() == "live" for s in secondary):
                    continue

            in_win, fresh = _mb_release_after_follow(rg["date"], followed_date, today)
            if not in_win:
                continue

            key = f"release:rg:{rg['rgid']}"
            if state.has_want(key):
                continue

            if lib is not None and lib.already_have(
                artist["name"],
                rg["album"],
                rgid=rg["rgid"],
                min_coverage=cfg.owned_coverage,
            ) is not None:
                continue

            try:
                tcount = mb.rg_track_count(rg["rgid"])
            except mb.MBUnavailable:
                tcount = None

            if lib is not None and tcount and lib.already_have(
                artist["name"],
                rg["album"],
                rgid=rg["rgid"],
                expected_tracks=tcount,
                min_coverage=cfg.owned_coverage,
            ) is not None:
                continue

            src = "drop" if (fresh and cfg.drop_window_hours) else f"release:{artist['name']}"
            new = state.upsert_want(
                key,
                artist["name"],
                rg["album"],
                rg["year"],
                tcount,
                src,
                cfg=cfg,
            )
            if new:
                stats["new_wants"] += 1
                if src == "drop":
                    wid = state.want_id_by_key(key)
                    if wid:
                        state.open_drop_window(wid, cfg.drop_window_hours * 3600)
                state.log_event(
                    "release",
                    artist["name"],
                    rg["album"],
                    f"new {rtype} ({rg['date']})"
                    + (" — STRIKE MODE, hunting now" if src == "drop" else " from followed artist — auto-wanted"),
                )
                log.info(
                    "NEW RELEASE%s: %s — %s (%s)",
                    " [DROP]" if src == "drop" else "",
                    artist["name"],
                    rg["album"],
                    rg["date"],
                )
        if hit_deadline:
            break
        state.mark_artist_checked(artist["mbid"])
        stats["checked"] += 1
    return stats


def mirror(
    cfg: HunterConfig,
    spotify: Any,
    state: HunterState,
    apply: bool,
    deadline: float,
) -> dict[str, int]:
    """Rebuild Navidrome playlists from Spotify cache."""
    from hunter.playlist_sync import SubsonicClient, mirror_playlists

    allowed = {
        pid
        for pid, (_hunt, mirror_on) in state.playlist_flags().items()
        if mirror_on
    }
    with SubsonicClient(cfg.navidrome_url, cfg.navidrome_user, cfg.navidrome_pass) as subsonic:
        stats = mirror_playlists(
            spotify,
            subsonic,
            state,
            exclude_re=cfg.playlist_exclude,
            prefix=cfg.playlist_prefix,
            lookup_cap=cfg.playlist_match_cap,
            apply=apply,
            deadline=deadline,
            allowed_ids=allowed,
            miss_retry_s=cfg.miss_retry_seconds,
        )
    return {
        "playlists": stats.playlists,
        "tracks": stats.tracks_total,
        "matched": stats.tracks_matched,
        "lookups": stats.lookups,
    }


def _daily_backup(cfg: HunterConfig) -> None:
    backups = cfg.state_dir / "backups"
    target = backups / f"hunter_{datetime.now():%Y%m%d}.db"
    if target.exists() or not cfg.state_db.exists():
        return
    try:
        db.backup_sqlite(cfg.state_db, target)
        db.prune_backups(backups, keep_days=14)
        log.info("state backup written: %s", target.name)
    except (sqlite3.Error, OSError) as exc:
        log.warning("state backup failed: %s", exc)


def _write_heartbeat(cfg: HunterConfig, status: str, extra: dict[str, Any]) -> None:
    payload = {"ts": int(time.time()), "status": status, **extra}
    try:
        domain.atomic_write_json(cfg.state_dir / "heartbeat.json", payload, indent=2)
    except OSError as exc:
        log.warning("heartbeat write failed: %s", exc)


def _grab_target_label(slskd_grabs: int, prowlarr_grabs: int) -> str:
    if slskd_grabs and prowlarr_grabs:
        return "slskd + Prowlarr"
    if prowlarr_grabs:
        return "Prowlarr"
    return "slskd"


def run_pass(
    cfg: HunterConfig,
    apply: bool | None = None,
    limit: int | None = None,
    verbose: bool = False,
    *,
    skip_spotify: bool = False,
    skip_hunt: bool = False,
    skip_playlists: bool = False,
    skip_artists: bool = False,
) -> int:
    """Execute one full bounded Hunter pass under ``hunter.lock``."""
    del verbose  # logging level configured by caller (CLI/test)
    eff_apply = cfg.apply if apply is None else bool(apply)
    eff_limit = limit if (limit is not None and limit > 0) else cfg.max_grabs_per_pass
    started_ts = time.time()
    deadline = started_ts + cfg.pass_deadline

    try:
        with hold(cfg.state_dir / "hunter.lock"):
            try:
                _daily_backup(cfg)
                notifier = Notifier(cfg.webhook_url, cfg.notify_min_interval, cfg.state_dir)
                summary: dict[str, Any] = {"apply": eff_apply}
                log.info("=== hunter pass start (apply=%s) ===", eff_apply)

                summary["storage"] = storage.summary(cfg)
                if not summary["storage"]["ok"]:
                    log.warning("storage degraded: %s", summary["storage"]["error"])

                with HunterState(cfg.state_db) as state:
                    lib: BeetsLibrary | None = None
                    try:
                        lib = BeetsLibrary(cfg.beets_db)
                    except (sqlite3.Error, OSError) as exc:
                        log.warning("beets DB unavailable (%s)", exc)

                    with ExitStack() as stack:
                        slskd_cm = (
                            stack.enter_context(SlskdClient(cfg.slskd_url, cfg.slskd_api_key))
                            if cfg.slskd_configured
                            else None
                        )
                        prowlarr_cm = (
                            stack.enter_context(
                                ProwlarrClient(cfg.prowlarr_url, cfg.prowlarr_api_key)
                            )
                            if cfg.prowlarr_configured and cfg.prowlarr_fallback
                            else None
                        )

                        if lib is not None:
                            summary["reconcile"] = reconcile(
                                cfg, state, lib, slskd_cm, notifier=notifier
                            )
                        else:
                            summary["reconcile"] = {"error": "beets db unavailable"}

                        apply_play_weights(cfg, state)

                        if not skip_hunt and slskd_cm is not None:
                            hunt_stats = hunt(
                                cfg,
                                state,
                                slskd_cm,
                                prowlarr_cm,
                                eff_apply,
                                eff_limit,
                                deadline,
                            )
                            if hunt_stats.get("transport_error", 0) >= 3 and "error" not in hunt_stats:
                                hunt_stats["error"] = "slskd unreachable"
                            summary["hunt"] = hunt_stats
                        elif not cfg.slskd_configured:
                            log.warning("slskd not configured — hunt skipped")

                    if not skip_spotify and cfg.spotify_configured and time.time() < deadline:
                        from hunter import spsync

                        cool = spsync.cooldown_remaining(state)
                        if cool > 0:
                            summary["sync"] = {"status": "cooldown", "cooldown_min": cool // 60}
                        else:
                            job = spsync.get_job(state)
                            active = job is not None and job.get("status") in (
                                "running",
                                "partial",
                                "error",
                            )
                            if active or spsync.needs_sync(state):
                                budget = min(600.0, max(0.0, deadline - time.time()))
                                if budget >= 60:
                                    job = spsync.run_sync_job(
                                        cfg, budget_s=budget, max_requests=40
                                    )
                                    summary["sync"] = spsync.job_summary(job)
                                else:
                                    summary["sync"] = {"status": "deferred — deadline near"}
                            else:
                                summary["sync"] = {
                                    "status": "fresh",
                                    "requests": (job or {}).get("requests", 0),
                                }
                    elif not cfg.spotify_configured:
                        summary["sync"] = "unconfigured"

                    if not skip_artists and time.time() < deadline:
                        summary["artists"] = watch_artists(cfg, state, lib, deadline)

                    if (
                        not skip_playlists
                        and cfg.spotify_configured
                        and cfg.sync_playlists
                        and cfg.navidrome_user
                        and time.time() < deadline
                    ):
                        from hunter import spsync

                        try:
                            summary["mirror"] = mirror(
                                cfg,
                                spsync.CachedSpotify(state),
                                state,
                                eff_apply,
                                deadline,
                            )
                        except Exception as exc:           # noqa: BLE001
                            msg = re.sub(r"\?[^\s'\"]*", "?<query stripped>", str(exc))
                            log.error("playlist mirror failed: %s", msg)
                            summary["mirror"] = {"error": msg}

                    reseeded = auto_reseed(cfg, state)
                    if reseeded:
                        summary["reseed"] = reseeded

                    summary["counts"] = state.counts()
                    log.info("=== pass done: %s ===", json.dumps(summary, default=str))
                    degraded = any(
                        isinstance(v, dict) and ("error" in v or v.get("ok") is False)
                        for v in summary.values()
                    )
                    _write_heartbeat(cfg, "degraded" if degraded else "ok", summary)
                    state.record_pass(
                        int(started_ts),
                        time.time() - started_ts,
                        eff_apply,
                        not degraded,
                        json.dumps(summary, default=str),
                    )

                    hunt_s = summary.get("hunt") if isinstance(summary.get("hunt"), dict) else {}
                    slskd_grabs = int(hunt_s.get("grabbed") or 0)
                    prowlarr_grabs = int(hunt_s.get("prowlarr") or 0)
                    total_grabs = slskd_grabs + prowlarr_grabs
                    if eff_apply and total_grabs > 0:
                        target = _grab_target_label(slskd_grabs, prowlarr_grabs)
                        sync_s = summary.get("sync") if isinstance(summary.get("sync"), dict) else {}
                        new_wants = int(sync_s.get("new") or 0)
                        notifier.send(
                            f"hunter: {new_wants} new want(s), {total_grabs} grabbed -> {target}",
                            tags=["hunter"],
                        )
                    hard_fail = (
                        not summary["storage"]["ok"]
                        or lib is None
                        or int(hunt_s.get("transport_error") or 0) >= 3
                    )
                    return 1 if hard_fail else 0
            except Exception as exc:
                log.exception("pass crashed (apply=%s): %s", eff_apply, exc)
                _write_heartbeat(cfg, "crashed", {"apply": eff_apply, "error": str(exc)})
                try:
                    with HunterState(cfg.state_db) as st:
                        st.record_pass(
                            int(started_ts),
                            time.time() - started_ts,
                            eff_apply,
                            False,
                            json.dumps({"apply": eff_apply, "error": str(exc)[:300]}),
                        )
                except Exception as rec_exc:              # noqa: BLE001
                    log.warning("failed to record crashed pass: %s", rec_exc)
                return 1
    except LockBusy:
        log.info("another hunter pass is running — exiting cleanly")
        return 3

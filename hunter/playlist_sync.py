"""Mirror Spotify playlists and create curated playlists in Navidrome.

For each (non-excluded) Spotify playlist: match its tracks to Navidrome
songs by search3 + normalized title/artist + duration tolerance, then
create-or-replace an owned Navidrome playlist via ``SubsonicClient``.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

from hunter.config import HunterConfig
from hunter.domain import norm_artist, normalize
from hunter.spotify import SpAlbum, SpPlaylist, SpTrack
from hunter.state import HunterState
from hunter.subsonic import HTTP_TIMEOUT, SubsonicClient, SubsonicError

log = logging.getLogger(__name__)

DURATION_TOLERANCE_S = 7
_MAX_CONSECUTIVE_LOOKUP_ERRORS = 3


class SpotifySource(Protocol):
    def playlists(self) -> list[SpPlaylist]: ...
    def playlist_tracks(self, playlist_id: str) -> list[SpTrack]: ...


@dataclass
class MirrorStats:
    playlists: int = 0
    tracks_total: int = 0
    tracks_matched: int = 0
    lookups: int = 0
    errors: int = 0


# Split multi-artist credits without breaking band names like "Florence and the
# Machine" or "Simon & Garfunkel".
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|;|/|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)
_TITLE_NOISE_RE = re.compile(
    r"\s*(?:[\(\[][^\)\]]*\b(?:feat|ft|featuring|with)\b[^\)\]]*[\)\]]"
    r"|-\s*(?:feat|ft|featuring|with)\.?\s+.*$"
    r"|[\(\[][^\)\]]*\bremaster(?:ed)?\b[^\)\]]*[\)\]]"
    r"|-\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*$"
    r"|[\(\[]\s*(?:explicit|clean|album version|single version|original mix)\s*[\)\]])",
    re.IGNORECASE,
)


def _artist_set(artist: str) -> set[str]:
    parts = {norm_artist(a) for a in _ARTIST_SPLIT_RE.split(artist or "")}
    parts.discard("")
    return parts


def _title_key(title: str) -> str:
    return normalize(_TITLE_NOISE_RE.sub("", title or ""))


def _artists_overlap(want: set[str], have: set[str]) -> bool:
    if not want or not have:
        return False
    if want & have:
        return True
    # Only allow word-boundary prefix matches on long multi-word names
    # (e.g. "lilly palmer" vs "lilly palmer dann"), never "pink" -> "pink floyd".
    return any(
        (h.startswith(w + " ") or w.startswith(h + " "))
        for w in want
        for h in have
        if len(w) >= 8 and len(h) >= 8
    )


def match_song(track: SpTrack, songs: list[dict]) -> str | None:
    """Exact match first (title + primary artist + duration); otherwise a
    relaxed match tolerating multi-artist strings and feat./remaster noise,
    guarded by the duration window when durations are known."""
    want_title = normalize(track.title)
    want_artist = norm_artist(track.artist)
    want_key = _title_key(track.title)
    if not want_key and not want_title:
        return None
    want_artists = _artist_set(track.artist)
    if want_artist:
        want_artists = want_artists | {want_artist}

    relaxed: list[tuple[int, int, str]] = []
    for s in songs:
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        title = s.get("title") or ""
        artist = s.get("artist") or ""
        try:
            dur = int(s.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0
        has_both_dur = bool(track.duration_s and dur)
        delta = abs(dur - track.duration_s) if has_both_dur else 0
        if has_both_dur and delta > DURATION_TOLERANCE_S:
            continue
        if (
            want_title
            and want_artist
            and normalize(title) == want_title
            and norm_artist(artist) == want_artist
        ):
            return str(s["id"])
        if not want_key or _title_key(title) != want_key:
            continue
        have_artist = norm_artist(artist)
        have_artists = _artist_set(artist)
        if have_artist:
            have_artists = have_artists | {have_artist}
        if not _artists_overlap(want_artists, have_artists):
            continue
        # Prefer matches with confirmed duration proximity over unknown-duration matches.
        relaxed.append((0 if has_both_dur else 1, delta, str(s["id"])))
    if relaxed:
        relaxed.sort()
        return relaxed[0][2]
    return None


def _resolve_track_checked(
    subsonic: SubsonicClient,
    state: HunterState,
    track: SpTrack,
    *,
    miss_retry_s: int = 259_200,
) -> tuple[str | None, bool, bool]:
    """Return ``(song_id, did_lookup, failed)``. Cache-first; falls back to
    title-only search whenever ``title artist`` returns no match."""
    cached, song_id = state.get_track_match(track.id, miss_retry_s=miss_retry_s)
    if cached:
        return song_id, False, False
    try:
        songs = subsonic.search_songs(f"{track.title} {track.artist}")
        song_id = match_song(track, songs)
        if song_id is None and track.artist:
            songs_title = subsonic.search_songs(track.title)
            song_id = match_song(track, songs_title)
    except SubsonicError as exc:
        log.warning("subsonic lookup failed for %r: %s", track.title, exc)
        return None, True, True
    state.set_track_match(track.id, song_id)
    return song_id, True, False


def resolve_track(
    subsonic: SubsonicClient,
    state: HunterState,
    track: SpTrack,
    *,
    miss_retry_s: int = 259_200,
) -> tuple[str | None, bool]:
    """Return ``(song_id, did_lookup)``."""
    song_id, did, _failed = _resolve_track_checked(
        subsonic, state, track, miss_retry_s=miss_retry_s
    )
    return song_id, did


def _load_owned_playlists(state: HunterState) -> dict[str, str]:
    try:
        raw = json.loads(state.get_meta("nd_playlists", "{}"))
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except ValueError:
        return {}


def _save_owned_playlists(state: HunterState, owned: dict[str, str]) -> None:
    state.set_meta("nd_playlists", json.dumps(owned))


def _resolve_owned_playlist(
    name: str,
    existing_multi: dict[str, list[str]],
    owned_pls: dict[str, str],
    *,
    overwrite: bool = False,
) -> tuple[str | None, bool]:
    """Return ``(existing_id, conflict)`` for writing a playlist named *name*.

    ``conflict`` is True when a non-Hunter playlist with *name* exists and
    *overwrite* is False.
    """
    all_ids = existing_multi.get(name, [])
    owned_id = owned_pls.get(name)
    if owned_id and owned_id in all_ids:
        return owned_id, False
    if all_ids:
        if overwrite:
            return all_ids[-1], False
        return None, True
    return None, False


def _persist_new_playlist_ownership(
    subsonic: SubsonicClient,
    state: HunterState,
    name: str,
    new_id: str | None,
    owned_pls: dict[str, str],
    existing_multi: dict[str, list[str]],
) -> str | None:
    resolved_id = new_id
    if not resolved_id:
        try:
            resolved_id = subsonic.playlists_multi().get(name, [None])[-1]
        except SubsonicError as exc:
            log.warning("playlist %r created but ID lookup failed: %s", name, exc)
            return None
    if resolved_id:
        owned_pls[name] = resolved_id
        existing_multi.setdefault(name, []).append(resolved_id)
        _save_owned_playlists(state, owned_pls)
    return resolved_id


def create_navidrome_playlist(
    cfg: HunterConfig,
    name: str,
    *,
    song_ids: list[str] | None = None,
    tracks: list[tuple[str, str]] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create or update a Navidrome playlist safely (respecting ownership).

    Accepts either pre-resolved Navidrome ``song_ids`` OR ``(artist, title)``
    pairs to search via Subsonic. Never holds ``HunterState`` open across
    network calls.
    """
    if not cfg.navidrome_user:
        return {"ok": False, "status": 400, "error": "Navidrome not configured"}
    clean_name = (name or "").strip()
    if not clean_name:
        return {"ok": False, "status": 400, "error": "playlist name required"}

    with HunterState(cfg.state_db) as st:
        owned_pls = _load_owned_playlists(st)

    try:
        with SubsonicClient(cfg.navidrome_url, cfg.navidrome_user, cfg.navidrome_pass) as sub:
            existing_multi = sub.playlists_multi()
            existing_id, conflict = _resolve_owned_playlist(
                clean_name, existing_multi, owned_pls, overwrite=overwrite
            )
            if conflict:
                return {
                    "ok": False,
                    "status": 409,
                    "error": (
                        f"playlist {clean_name!r} already exists in Navidrome "
                        "and was not created by hunter — pass overwrite=true to replace it"
                    ),
                }

            resolved_ids: list[str] = []
            missed: list[str] = []
            if song_ids is not None:
                resolved_ids = [str(s) for s in song_ids if s]
            elif tracks is not None:
                seen_ids: set[str] = set()
                for artist, title in tracks[:300]:
                    if not title:
                        continue
                    fake = SpTrack(
                        id="",
                        title=title,
                        artist=artist,
                        duration_s=0,
                        album=SpAlbum("", artist, "", 0, 0),
                    )
                    q = f"{title} {artist}".strip()
                    sid = match_song(fake, sub.search_songs(q))
                    if sid is None and artist:
                        sid = match_song(fake, sub.search_songs(title))
                    if sid and sid not in seen_ids:
                        seen_ids.add(sid)
                        resolved_ids.append(sid)
                    elif not sid:
                        missed.append(f"{artist} — {title}" if artist else title)

            if not resolved_ids:
                return {
                    "ok": False,
                    "status": 404,
                    "error": "none of those tracks matched in Navidrome",
                }

            new_id = sub.create_or_replace(clean_name, resolved_ids, existing_id)
            if not existing_id and not new_id:
                try:
                    new_id = sub.playlists_multi().get(clean_name, [None])[-1]
                except SubsonicError:
                    new_id = None
    except SubsonicError as exc:
        return {"ok": False, "status": 502, "error": f"navidrome: {exc}"}

    if new_id:
        with HunterState(cfg.state_db) as st:
            fresh_owned = _load_owned_playlists(st)
            fresh_owned[clean_name] = new_id
            _save_owned_playlists(st, fresh_owned)

    total_in = len(song_ids) if song_ids is not None else len(tracks or ())
    return {
        "ok": True,
        "name": clean_name,
        "matched": len(resolved_ids),
        "tracks": len(resolved_ids),
        "total": total_in,
        "missed": len(missed),
        "missed_sample": missed[:15],
        "replaced": bool(existing_id),
    }


def mirror_playlists(
    spotify: SpotifySource,
    subsonic: SubsonicClient,
    state: HunterState,
    *,
    exclude_re: str,
    prefix: str,
    lookup_cap: int,
    apply: bool,
    deadline: float,
    allowed_ids: set[str] | None = None,
    miss_retry_s: int = 259_200,
) -> MirrorStats:
    stats = MirrorStats()
    exclude = re.compile(exclude_re, re.IGNORECASE) if exclude_re else None
    existing_multi = subsonic.playlists_multi()
    owned_pls = _load_owned_playlists(state)
    seen_target_names: set[str] = set()

    for playlist in spotify.playlists():
        if time.time() > deadline:
            log.warning("pass deadline hit — stopping playlist mirror early")
            break
        if exclude and exclude.search(playlist.name):
            continue
        if allowed_ids is not None and playlist.id not in allowed_ids:
            continue

        name = prefix + playlist.name
        if name in seen_target_names:
            log.warning(
                "duplicate Spotify playlist name %r (id=%s) — skipping second copy "
                "so it does not overwrite the first",
                name,
                playlist.id,
            )
            continue
        seen_target_names.add(name)

        try:
            tracks = spotify.playlist_tracks(playlist.id)
        except Exception as exc:                       # noqa: BLE001
            stats.errors += 1
            log.warning("failed reading tracks for playlist %r: %s", playlist.name, exc)
            continue

        song_ids: list[str] = []
        truncated = False
        consec_errors = 0
        for track in tracks:
            if (
                stats.lookups >= lookup_cap
                or time.time() > deadline
                or consec_errors >= _MAX_CONSECUTIVE_LOOKUP_ERRORS
            ):
                cached, song_id = state.get_track_match(
                    track.id, miss_retry_s=miss_retry_s
                )
                if not cached:
                    truncated = True
                did = False
            else:
                song_id, did, failed = _resolve_track_checked(
                    subsonic, state, track, miss_retry_s=miss_retry_s
                )
                if failed:
                    truncated = True
                    stats.errors += 1
                    consec_errors += 1
                else:
                    consec_errors = 0
            stats.lookups += did
            if song_id:
                song_ids.append(song_id)

        stats.playlists += 1
        stats.tracks_total += len(tracks)
        stats.tracks_matched += len(song_ids)

        if truncated:
            log.info(
                "playlist %r: resolution cut short by cap/deadline/error — skipping write "
                "to avoid truncating Navidrome playlist",
                name,
            )
            continue
        if not song_ids:
            log.info("playlist %r: 0/%d matched — skipping write", name, len(tracks))
            continue

        existing_id, conflict = _resolve_owned_playlist(name, existing_multi, owned_pls)
        if conflict:
            log.warning(
                "playlist %r exists in Navidrome but hunter didn't create it — "
                "refusing to overwrite (rename the Spotify playlist or set "
                "HUNTER_PLAYLIST_PREFIX)",
                name,
            )
            continue

        if apply:
            try:
                new_id = subsonic.create_or_replace(name, song_ids, existing_id)
                log.info("playlist %r: wrote %d/%d tracks", name, len(song_ids), len(tracks))
                if not existing_id:
                    _persist_new_playlist_ownership(
                        subsonic, state, name, new_id, owned_pls, existing_multi
                    )
            except SubsonicError as exc:
                stats.errors += 1
                log.warning("playlist %r write failed: %s", name, exc)
                err_s = str(exc).lower()
                if (
                    exc.code == 70
                    or "not found" in err_s
                    or "'code': 70" in err_s
                    or '"code": 70' in err_s
                ) and hasattr(state, "invalidate_track_match"):
                    with state.transaction():
                        for t in tracks:
                            state.invalidate_track_match(t.id)
        else:
            log.info(
                "[dry-run] playlist %r: would write %d/%d tracks",
                name,
                len(song_ids),
                len(tracks),
            )
    return stats


__all__ = [
    "DURATION_TOLERANCE_S",
    "HTTP_TIMEOUT",
    "MirrorStats",
    "SpotifySource",
    "SubsonicClient",
    "SubsonicError",
    "create_navidrome_playlist",
    "match_song",
    "mirror_playlists",
    "resolve_track",
]

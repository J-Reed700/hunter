"""Mirror Spotify playlists into Navidrome via the Subsonic API.

For each (non-excluded) Spotify playlist: match its tracks to Navidrome
songs by search3 + normalized title/artist + duration tolerance, then
create-or-replace a Navidrome playlist via POST /rest/createPlaylist
(form-encoded so 500+ track playlists never hit HTTP 414).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol

from hunter.domain import norm_artist, normalize
from hunter.http_client import HttpError, HttpSession, default_user_agent
from hunter.spotify import SpPlaylist, SpTrack
from hunter.state import HunterState

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 30
DURATION_TOLERANCE_S = 7


class SubsonicError(RuntimeError):
    pass


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


class SubsonicClient:
    def __init__(self, url: str, user: str, password: str, *, timeout: float = HTTP_TIMEOUT):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self._http = HttpSession(
            timeout=timeout,
            user_agent=default_user_agent(component="subsonic"),
        )

    def __enter__(self) -> "SubsonicClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _auth(self) -> dict[str, str]:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {"u": self.user, "t": token, "s": salt}

    def _call(self, endpoint: str, params: dict[str, Any], *, method: str = "GET") -> dict:
        merged: dict[str, Any] = {
            **self._auth(),
            "v": "1.16.1",
            "c": "hunter",
            "f": "json",
            **params,
        }
        url = f"{self.url}/rest/{endpoint}"
        try:
            if method.upper() == "POST":
                data = self._http.post_json(url, data=merged)
            else:
                data = self._http.get_json(url, params=merged)
        except HttpError as exc:
            # Never embed full URL (which may carry auth query params).
            raise SubsonicError(f"subsonic {endpoint}: {type(exc).__name__}") from None
        if not isinstance(data, dict):
            raise SubsonicError(f"subsonic {endpoint}: invalid response")
        body = data.get("subsonic-response")
        if not isinstance(body, dict):
            raise SubsonicError(f"subsonic {endpoint}: missing subsonic-response")
        if body.get("status") != "ok":
            raise SubsonicError(f"subsonic {endpoint}: {body.get('error')}")
        return body

    def search_songs(self, query: str, count: int = 20) -> list[dict]:
        body = self._call(
            "search3",
            {
                "query": query,
                "songCount": count,
                "artistCount": 0,
                "albumCount": 0,
            },
        )
        sr = body.get("searchResult3")
        songs = sr.get("song") if isinstance(sr, dict) else []
        if isinstance(songs, dict):
            return [songs]
        return [s for s in (songs or []) if isinstance(s, dict)]

    def playlists_multi(self) -> dict[str, list[str]]:
        """Return ``{playlist_name: [id1, id2, ...]}`` preserving duplicates."""
        body = self._call("getPlaylists", {})
        pls_obj = body.get("playlists")
        pls = pls_obj.get("playlist") if isinstance(pls_obj, dict) else []
        if isinstance(pls, dict):
            pls = [pls]
        out: dict[str, list[str]] = {}
        for p in pls or []:
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            pid = p.get("id")
            if name is not None and pid is not None:
                out.setdefault(str(name), []).append(str(pid))
        return out

    def playlists(self) -> dict[str, str]:
        return {name: ids[-1] for name, ids in self.playlists_multi().items() if ids}

    def create_or_replace(
        self,
        name: str,
        song_ids: list[str],
        existing_id: str | None = None,
    ) -> str | None:
        """Create or replace a Navidrome playlist via POST form data (avoids
        HTTP 414 on large playlists). Returns the Navidrome playlist ID."""
        params: dict[str, Any] = {"songId": song_ids}
        if existing_id:
            params["playlistId"] = existing_id
        else:
            params["name"] = name
        body = self._call("createPlaylist", params, method="POST")
        pl = body.get("playlist")
        if isinstance(pl, dict) and pl.get("id") is not None:
            return str(pl["id"])
        return existing_id


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
    guarded by the duration window."""
    want_title = normalize(track.title)
    want_artist = norm_artist(track.artist)
    want_key = _title_key(track.title)
    if not want_key and not want_title:
        return None
    want_artists = _artist_set(track.artist)
    if want_artist:
        want_artists = want_artists | {want_artist}

    relaxed: list[tuple[int, str]] = []
    for s in songs:
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        title = s.get("title") or ""
        artist = s.get("artist") or ""
        dur = int(s.get("duration") or 0)
        delta = abs(dur - track.duration_s) if (track.duration_s and dur) else 0
        if delta > DURATION_TOLERANCE_S:
            continue
        if want_title and want_artist and normalize(title) == want_title and norm_artist(artist) == want_artist:
            return str(s["id"])
        if not want_key or _title_key(title) != want_key:
            continue
        have_artist = norm_artist(artist)
        have_artists = _artist_set(artist)
        if have_artist:
            have_artists = have_artists | {have_artist}
        if not _artists_overlap(want_artists, have_artists):
            continue
        if not (track.duration_s and dur):
            continue
        relaxed.append((delta, str(s["id"])))
    if relaxed:
        relaxed.sort()
        return relaxed[0][1]
    return None


_match_song = match_song


def _resolve_track_checked(
    subsonic: SubsonicClient,
    state: HunterState,
    track: SpTrack,
    *,
    miss_retry_s: int = 7 * 86_400,
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
    miss_retry_s: int = 7 * 86_400,
) -> tuple[str | None, bool]:
    """Return ``(song_id, did_lookup)``."""
    song_id, did, _failed = _resolve_track_checked(
        subsonic, state, track, miss_retry_s=miss_retry_s
    )
    return song_id, did


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
    miss_retry_s: int = 7 * 86_400,
) -> MirrorStats:
    stats = MirrorStats()
    exclude = re.compile(exclude_re, re.IGNORECASE) if exclude_re else None
    existing_multi = subsonic.playlists_multi()
    try:
        owned_raw = json.loads(state.get_meta("nd_playlists", "{}"))
        owned_pls: dict[str, str] = owned_raw if isinstance(owned_raw, dict) else {}
    except ValueError:
        owned_pls = {}

    for playlist in spotify.playlists():
        if time.time() > deadline:
            log.warning("pass deadline hit — stopping playlist mirror early")
            break
        if exclude and exclude.search(playlist.name):
            continue
        if allowed_ids is not None and playlist.id not in allowed_ids:
            continue

        try:
            tracks = spotify.playlist_tracks(playlist.id)
        except Exception as exc:                       # noqa: BLE001
            stats.errors += 1
            log.warning("failed reading tracks for playlist %r: %s", playlist.name, exc)
            continue

        song_ids: list[str] = []
        truncated = False
        for track in tracks:
            if stats.lookups >= lookup_cap or time.time() > deadline:
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
            stats.lookups += did
            if song_id:
                song_ids.append(song_id)

        stats.playlists += 1
        stats.tracks_total += len(tracks)
        stats.tracks_matched += len(song_ids)

        name = prefix + playlist.name
        if truncated:
            log.info(
                "playlist %r: resolution cut short by cap/deadline — skipping write "
                "to avoid truncating Navidrome playlist",
                name,
            )
            continue
        if not song_ids:
            log.info("playlist %r: 0/%d matched — skipping write", name, len(tracks))
            continue

        all_ids = existing_multi.get(name, [])
        owned_id = owned_pls.get(name)
        if owned_id and owned_id in all_ids:
            existing_id: str | None = owned_id
        elif all_ids:
            log.warning(
                "playlist %r exists in Navidrome but hunter didn't create it — "
                "refusing to overwrite (rename the Spotify playlist or set "
                "HUNTER_PLAYLIST_PREFIX)",
                name,
            )
            continue
        else:
            existing_id = None

        if apply:
            try:
                new_id = subsonic.create_or_replace(name, song_ids, existing_id)
                log.info("playlist %r: wrote %d/%d tracks", name, len(song_ids), len(tracks))
                if not existing_id:
                    if not new_id:
                        new_id = subsonic.playlists_multi().get(name, [None])[-1]
                    if new_id:
                        owned_pls[name] = new_id
                        existing_multi.setdefault(name, []).append(new_id)
                        state.set_meta("nd_playlists", json.dumps(owned_pls))
            except SubsonicError as exc:
                stats.errors += 1
                log.warning("playlist %r write failed: %s", name, exc)
                err_s = str(exc).lower()
                if ("not found" in err_s or "'code': 70" in err_s or '"code": 70' in err_s) and hasattr(state, "invalidate_track_match"):
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

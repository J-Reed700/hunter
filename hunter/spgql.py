"""Spotify library reads via the web player's own GraphQL (pathfinder).

Persisted-query hashes rotate with web-player deploys, so we extract them
from Spotify's official web-player bundles, cache them atomically in
``sp_ops.json``, and self-heal on HTTP 400/404 OR HTTP 200 GraphQL
``PersistedQueryNotFound`` errors.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from hunter.domain import atomic_write_json, extract_year
from hunter.http_client import HttpError, HttpSession
from hunter.spotify import (
    WEB_UA,
    SpotifyClient,
    SpotifyError,
    paced_spotify_call,
)

log = logging.getLogger(__name__)

GQL_URL = "https://api-partner.spotify.com/pathfinder/v2/query"
OPEN_URL = "https://open.spotify.com/"
_OP_RE = re.compile(r'new \w+\.\w+\("(\w+)","(?:query|mutation)",'
                    r'"([0-9a-f]{64})"')
HTTP_TIMEOUT = 30
_MAX_BUNDLE_BYTES = 15 * 1024 * 1024
_ALLOWED_BUNDLE_HOSTS = frozenset({"open.spotify.com", "open.spotifycdn.com"})

OP_LIBRARY = "libraryV3"            # roster/saved albums/followed artists
OP_LIKED = "fetchLibraryTracks"     # liked songs
OP_PLAYLIST = "fetchPlaylist"       # one playlist's contents

PAGE_SIZE = 50


def _as_dict(obj: Any, *keys: str) -> dict:
    cur: Any = obj
    for k in keys:
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(k)
    return cur if isinstance(cur, dict) else {}


def _as_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _is_allowed_bundle_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in _ALLOWED_BUNDLE_HOSTS
    )


def _is_persisted_query_not_found(errors: Any) -> bool:
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        msg = str(err.get("message") or "").lower()
        code = str((err.get("extensions") or {}).get("code") or "").lower()
        if "persistedquerynotfound" in msg or code == "persisted_query_not_found":
            return True
    return False


class SpotifyGQL:
    """Read-only pathfinder client. Shares SpotifyClient's sp_dc token mint;
    paces itself and honors 429/Retry-After cleanly."""

    def __init__(self, spotify: SpotifyClient, state_dir: Path):
        self._spotify = spotify
        self._ops_file = Path(state_dir) / "sp_ops.json"
        self._http = HttpSession(timeout=HTTP_TIMEOUT, user_agent=WEB_UA)
        self._http.proxies.update(spotify.proxies)
        self._ops: dict[str, str] = {}
        self._last_call = 0.0
        self._pace = 0.8
        self._rl_attempts = 6

    def __enter__(self) -> "SpotifyGQL":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def pace(self) -> float:
        return self._pace

    @pace.setter
    def pace(self, value: float) -> None:
        self._pace = max(0.0, float(value))

    # --- persisted-query hash manifest -------------------------------------

    def _download_bundle_text(self, url: str) -> str | None:
        if not _is_allowed_bundle_url(url):
            log.debug("skipping untrusted bundle URL: %s", url)
            return None
        try:
            resp = self._http.get(
                url,
                headers={"User-Agent": WEB_UA},
                timeout=60,
                raise_for_status=False,
                stream=True,
            )
        except HttpError:
            return None
        try:
            if resp.status_code != 200:
                return None
            final_url = getattr(resp, "url", None)
            if isinstance(final_url, str) and final_url and not _is_allowed_bundle_url(final_url):
                log.warning("skipping redirected untrusted bundle URL: %s", final_url)
                return None
            cl = (resp.headers or {}).get("Content-Length")
            if cl:
                try:
                    if int(cl) > _MAX_BUNDLE_BYTES:
                        log.warning("skipping oversized bundle (%s bytes): %s", cl, url)
                        return None
                except ValueError:
                    pass
            if type(resp) is requests.Response:
                chunks: list[bytes] = []
                total = 0
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > _MAX_BUNDLE_BYTES:
                        log.warning("bundle exceeded 15 MB cap: %s", url)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
            content = getattr(resp, "content", None)
            if isinstance(content, (bytes, bytearray)) and len(content) > _MAX_BUNDLE_BYTES:
                return None
            text = resp.text or ""
            if len(text.encode("utf-8", errors="replace")) > _MAX_BUNDLE_BYTES:
                return None
            return text
        finally:
            close_fn = getattr(resp, "close", None)
            if callable(close_fn):
                close_fn()

    def _extract_ops(self) -> dict[str, str]:
        """Pull current persisted-query hashes out of the live web-player bundles."""
        try:
            resp = self._http.get(
                OPEN_URL,
                headers={"User-Agent": WEB_UA},
                timeout=HTTP_TIMEOUT,
                raise_for_status=False,
            )
        except HttpError as exc:
            raise SpotifyError(f"web-player fetch failed: {exc}") from exc
        if resp.status_code != 200:
            raise SpotifyError(
                f"web-player fetch failed: HTTP {resp.status_code}"
            )
        html = resp.text or ""
        bundles = re.findall(r'src="(https://[^"]+\.js)"', html)
        ops: dict[str, str] = {}
        for url in bundles:
            if OP_PLAYLIST in ops and OP_LIBRARY in ops and OP_LIKED in ops:
                break
            js = self._download_bundle_text(url)
            if not js:
                continue
            for name, h in _OP_RE.findall(js):
                ops.setdefault(name, h)
        missing = [op for op in (OP_LIBRARY, OP_LIKED, OP_PLAYLIST) if op not in ops]
        if missing:
            raise SpotifyError(
                f"web-player bundle no longer exposes {missing} — "
                "Spotify restructured the client (needs code update)"
            )
        atomic_write_json(self._ops_file, ops, indent=2)
        log.info("extracted %d persisted-query hashes from web player", len(ops))
        return ops

    def _get_ops(self, refresh: bool = False) -> dict[str, str]:
        if refresh or not self._ops:
            try:
                if not refresh:
                    loaded = json.loads(self._ops_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        self._ops = loaded
            except (OSError, ValueError):
                pass
            if not isinstance(self._ops, dict):
                self._ops = {}
            if refresh or not self._ops:
                self._ops = self._extract_ops()
        return self._ops

    # --- transport ----------------------------------------------------------

    def _query(self, op: str, variables: dict, _refreshed: bool = False) -> dict:
        ops = self._get_ops()
        if op not in ops:
            if _refreshed:
                raise SpotifyError(f"persisted op {op} unknown after refresh")
            self._get_ops(refresh=True)
            return self._query(op, variables, _refreshed=True)

        refresh_needed = False

        def _handle_non_200(resp: Any) -> Any:
            nonlocal refresh_needed
            if resp.status_code in (400, 404) and not _refreshed:
                log.info(
                    "persisted query %s rejected (%s) — refreshing hashes",
                    op,
                    resp.status_code,
                )
                refresh_needed = True
                return resp
            return None

        resp = paced_spotify_call(
            label=f"pathfinder {op}",
            attempts=self._rl_attempts if not _refreshed else max(1, self._rl_attempts // 2),
            pace_floor=0.8,
            get_pace=lambda: self._pace,
            set_pace=lambda v: setattr(self, "_pace", v),
            get_last_call=lambda: self._last_call,
            set_last_call=lambda v: setattr(self, "_last_call", v),
            get_token=lambda force: self._spotify.get_access_token(force_refresh=force),
            do_request=lambda token: self._http.post(
                GQL_URL,
                json={
                    "operationName": op,
                    "variables": variables,
                    "extensions": {
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": ops[op],
                        }
                    },
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": WEB_UA,
                    "App-Platform": "WebPlayer",
                    "Origin": "https://open.spotify.com",
                    "Referer": "https://open.spotify.com/",
                },
                timeout=HTTP_TIMEOUT,
                raise_for_status=False,
            ),
            on_non_200=_handle_non_200,
        )
        if refresh_needed:
            self._get_ops(refresh=True)
            return self._query(op, variables, _refreshed=True)

        try:
            body = resp.json()
        except ValueError as exc:
            raise SpotifyError(
                f"pathfinder {op}: non-JSON response ({exc})"
            ) from exc
        if not isinstance(body, dict):
            raise SpotifyError(f"pathfinder {op}: expected JSON object")

        if body.get("errors"):
            if _is_persisted_query_not_found(body["errors"]) and not _refreshed:
                log.info(
                    "persisted query %s rejected (PersistedQueryNotFound) — refreshing hashes",
                    op,
                )
                self._get_ops(refresh=True)
                return self._query(op, variables, _refreshed=True)
            raise SpotifyError(
                f"pathfinder {op}: {json.dumps(body['errors'])[:200]}"
            )

        data = body.get("data")
        return data if isinstance(data, dict) else {}

    # --- pages ---------------------------------------------------------------

    def library_page(
        self, filter_: str, offset: int, limit: int = PAGE_SIZE
    ) -> tuple[list[dict], int]:
        data = self._query(
            OP_LIBRARY,
            {
                "filters": [filter_],
                "order": None,
                "textFilter": "",
                "limit": limit,
                "offset": offset,
                "flatten": False,
                "expandedFolders": [],
                "folderUri": None,
                "includeFoldersWhenFlattening": True,
            },
        )
        lib = _as_dict(data, "me", "libraryV3")
        items = lib.get("items") or []
        return (items if isinstance(items, list) else []), _as_int(lib.get("totalCount"))

    def liked_page(
        self, offset: int, limit: int = PAGE_SIZE
    ) -> tuple[list[dict], int]:
        data = self._query(OP_LIKED, {"offset": offset, "limit": limit})
        tracks = _as_dict(data, "me", "library", "tracks")
        items = tracks.get("items") or []
        return (items if isinstance(items, list) else []), _as_int(tracks.get("totalCount"))

    def playlist_page(
        self, playlist_id: str, offset: int, limit: int = 100
    ) -> tuple[list[dict], int]:
        data = self._query(
            OP_PLAYLIST,
            {
                "uri": f"spotify:playlist:{playlist_id}",
                "offset": offset,
                "limit": limit,
                "enableWatchFeedEntrypoint": False,
            },
        )
        content = _as_dict(data, "playlistV2", "content")
        items = content.get("items") or []
        return (items if isinstance(items, list) else []), _as_int(content.get("totalCount"))


# --- item parsers (pathfinder shapes → plain tuples/dicts) -------------------


def _uri_id(uri: str) -> str:
    return (uri or "").rsplit(":", 1)[-1]


def _first_gql_artist(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    for entry in items:
        if not isinstance(entry, dict):
            continue
        prof = entry.get("profile")
        if isinstance(prof, dict):
            name = prof.get("name")
            if isinstance(name, str) and name:
                return name
    return ""


def parse_library_playlist(item: dict | None) -> tuple[str, str, str, int] | None:
    """libraryV3 Playlists item → (playlist_id, name, owner, track_count)."""
    if not isinstance(item, dict):
        return None
    it = item.get("item")
    if not isinstance(it, dict):
        return None
    pid = _uri_id(str(it.get("_uri") or it.get("uri") or ""))
    d = it.get("data")
    if not pid or not isinstance(d, dict):
        return None
    owner_v2 = d.get("ownerV2")
    owner_data = owner_v2.get("data") if isinstance(owner_v2, dict) else None
    owner = (owner_data.get("name") or "") if isinstance(owner_data, dict) else ""
    contents = d.get("contents")
    count = _as_int(contents.get("totalCount") if isinstance(contents, dict) else 0)
    return pid, str(d.get("name") or pid), str(owner), count


def parse_library_album(item: dict | None) -> tuple[str, str, str, int] | None:
    """libraryV3 Albums item → (album_id, artist, name, year)."""
    if not isinstance(item, dict):
        return None
    it = item.get("item")
    if not isinstance(it, dict):
        return None
    aid = _uri_id(str(it.get("_uri") or it.get("uri") or ""))
    d = it.get("data")
    if not aid or not isinstance(d, dict):
        return None
    artists_obj = d.get("artists")
    artist = _first_gql_artist(
        artists_obj.get("items") if isinstance(artists_obj, dict) else None
    )
    date_obj = d.get("date")
    date = str(date_obj.get("isoString") or "") if isinstance(date_obj, dict) else ""
    year = extract_year(date)
    return aid, artist, str(d.get("name") or ""), year


def parse_library_artist(item: dict | None) -> tuple[str, str] | None:
    """libraryV3 Artists item → (artist_id, name)."""
    if not isinstance(item, dict):
        return None
    it = item.get("item")
    if not isinstance(it, dict):
        return None
    arid = _uri_id(str(it.get("_uri") or it.get("uri") or ""))
    d = it.get("data")
    if not arid or not isinstance(d, dict):
        return None
    prof = d.get("profile")
    name = (prof.get("name") or "") if isinstance(prof, dict) else ""
    return (arid, str(name)) if name else None


def parse_track(wrapper: dict | None) -> dict | None:
    """A TrackResponseWrapper (fetchPlaylist itemV2 / fetchLibraryTracks
    .track) → flat track dict for the sp_tracks cache.

    Strictly requires ``uri.startswith("spotify:track:")`` so local files and
    podcast episodes are cleanly skipped.
    """
    if not isinstance(wrapper, dict):
        return None
    d = wrapper.get("data")
    if not isinstance(d, dict):
        return None
    uri = str(wrapper.get("_uri") or d.get("uri") or "")
    if not uri.startswith("spotify:track:"):
        return None
    tid = _uri_id(uri)
    if not tid:
        return None

    album = d.get("albumOfTrack")
    if not isinstance(album, dict):
        album = {}

    t_artists_obj = d.get("artists")
    a_artists_obj = album.get("artists")
    t_artist = _first_gql_artist(
        t_artists_obj.get("items") if isinstance(t_artists_obj, dict) else None
    )
    a_artist = _first_gql_artist(
        a_artists_obj.get("items") if isinstance(a_artists_obj, dict) else None
    )
    artist = t_artist or a_artist
    album_artist = a_artist or t_artist

    date_obj = album.get("date")
    date = str(date_obj.get("isoString") or "") if isinstance(date_obj, dict) else ""
    year = extract_year(date)

    dur_ms = 0
    for key in ("trackDuration", "duration"):
        val = d.get(key)
        if isinstance(val, dict) and val.get("totalMilliseconds"):
            dur_ms = _as_int(val["totalMilliseconds"])
            break

    tracks_obj = album.get("tracks")
    album_total = 0
    if isinstance(tracks_obj, dict) and tracks_obj.get("totalCount"):
        album_total = _as_int(tracks_obj["totalCount"])
    elif album.get("totalTracks"):
        album_total = _as_int(album["totalTracks"])

    return {
        "track_id": tid,
        "title": str(d.get("name") or ""),
        "artist": artist,
        "album_artist": album_artist,
        "album": str(album.get("name") or ""),
        "album_id": _uri_id(str(album.get("uri") or "")),
        "year": year,
        "duration_s": int(dur_ms / 1000),
        "album_total": album_total,
    }

"""Minimal Spotify Web API client — sp_dc + refresh-token auth, hard timeouts.

No spotipy. Every HTTP call uses a context-managed HttpSession with explicit
timeouts, 429s honor Retry-After (via http_client.parse_retry_after) without
double-sleeping or negative-sleep crashes, and rotated refresh tokens are
persisted via config.save_spotify_token (atomic 0o600 merge-write).
"""
from __future__ import annotations

import email.utils
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import urllib.parse
from datetime import timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hunter import config
from hunter.domain import extract_year
from hunter.http_client import (
    HttpError,
    HttpSession,
    default_user_agent,
    parse_retry_after,
)

log = logging.getLogger(__name__)

API = "https://api.spotify.com/v1"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
OAUTH_REDIRECT_URI = "http://127.0.0.1:8888/callback"
OAUTH_SCOPES = "user-library-read playlist-read-private playlist-read-collaborative"
HTTP_TIMEOUT = 30
MAX_PAGES = 40          # 40 * 50 items — bound runaway pagination

WEB_TOKEN_URL = "https://open.spotify.com/api/token"
_TOTP_VER = 61
_TOTP_CIPHER = [44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97,
                75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78]

WEB_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
          " (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_WEB_UA = WEB_UA

# Max in-request sleep on HTTP 429 before failing fast with the full server Retry-After.
_MAX_IN_REQUEST_RL_SLEEP = 90.0
_FAIL_FAST_RL_THRESHOLD = 120.0
_MAX_RETRY_AFTER_REPORT = 86_400.0


def oauth_authorize_url(client_id: str, *, state: str = "") -> str:
    """Build the Spotify OAuth authorization URL (shared by CLI & Web UI)."""
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
    }
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def extract_oauth_code(raw: str) -> str:
    """Extract the authorization ``code`` from either a pasted callback URL or raw code."""
    s = (raw or "").strip()
    if not s:
        return ""
    if "code=" in s:
        parsed = urllib.parse.urlparse(s)
        vals = urllib.parse.parse_qs(parsed.query).get("code")
        if vals and vals[0]:
            return vals[0].strip()
    return s


def oauth_exchange_code(
    client_id: str,
    client_secret: str,
    code_or_url: str,
    *,
    cfg: Any = None,
    proxy: str = "",
) -> dict[str, Any]:
    """Exchange a Spotify OAuth authorization code for tokens (shared by CLI & Web UI)."""
    code = extract_oauth_code(code_or_url)
    if not code:
        raise SpotifyError("no authorization code provided")
    with HttpSession(
        timeout=HTTP_TIMEOUT,
        user_agent=default_user_agent(cfg, component="spotify"),
        proxy=proxy or None,
    ) as http:
        try:
            resp = http.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": OAUTH_REDIRECT_URI,
                },
                auth=(client_id, client_secret),
                timeout=HTTP_TIMEOUT,
                raise_for_status=False,
            )
        except HttpError as exc:
            raise SpotifyError(f"token exchange failed: {exc}") from exc
        if resp.status_code != 200:
            raise SpotifyError(f"token exchange failed: {resp.status_code} {resp.text[:200]}")
        try:
            tok = resp.json()
        except ValueError as exc:
            raise SpotifyError(f"token exchange invalid JSON: {exc}") from exc
    if not isinstance(tok, dict) or not tok.get("refresh_token"):
        raise SpotifyError("token exchange response missing refresh_token")
    return tok


def paced_spotify_call(
    *,
    label: str,
    attempts: int,
    pace_floor: float,
    get_pace: Callable[[], float],
    set_pace: Callable[[float], None],
    get_last_call: Callable[[], float],
    set_last_call: Callable[[float], None],
    get_token: Callable[[bool], str],
    do_request: Callable[[str], Any],
    on_non_200: Callable[[Any], Any] | None = None,
) -> Any:
    """Shared adaptive pacing + 429 Retry-After + single 401 refresh loop for
    both Spotify Web API (``SpotifyClient``) and Pathfinder GraphQL (``SpotifyGQL``)."""
    tried_401 = False
    last_ra: float | None = None
    last_status: int | None = None

    for attempt in range(max(1, int(attempts))):
        last_call = get_last_call()
        if last_call > 0.0:
            gap = get_pace() - (time.monotonic() - last_call)
            if gap > 0:
                time.sleep(gap)
        token = get_token(False)
        try:
            resp = do_request(token)
        except HttpError as exc:
            raise SpotifyError(f"{label}: {exc}") from exc
        set_last_call(time.monotonic())
        last_status = resp.status_code

        if resp.status_code == 429:
            raw_ra = parse_retry_after(
                resp.headers.get("Retry-After"),
                default=5.0,
                cap=_MAX_RETRY_AFTER_REPORT,
            )
            last_ra = raw_ra
            if raw_ra > _FAIL_FAST_RL_THRESHOLD:
                set_pace(min(max(get_pace() * 1.5, _MAX_IN_REQUEST_RL_SLEEP), _MAX_IN_REQUEST_RL_SLEEP))
                raise SpotifyRateLimitError(
                    f"{label}: rate-limited (Retry-After {int(raw_ra)}s — failing fast)",
                    retry_after=raw_ra,
                )
            wait = min(raw_ra, _MAX_IN_REQUEST_RL_SLEEP)
            set_pace(min(max(get_pace() * 1.5, wait), _MAX_IN_REQUEST_RL_SLEEP))
            log.info(
                "%s 429 — pace floor now %.1fs [%d/%d]",
                label,
                get_pace(),
                attempt + 1,
                attempts,
            )
            continue

        if resp.status_code == 401 and not tried_401:
            tried_401 = True
            get_token(True)
            continue

        if resp.status_code != 200:
            if on_non_200 is not None:
                handled = on_non_200(resp)
                if handled is not None:
                    return handled
            raise SpotifyError(f"{label}: {resp.status_code} {resp.text[:200]}")

        set_pace(max(pace_floor, get_pace() * 0.9))
        return resp

    if last_status == 429:
        raise SpotifyRateLimitError(
            f"{label}: rate-limited past {attempts} retries (Spotify web-token quota)",
            retry_after=last_ra,
        )
    raise SpotifyError(f"{label}: exhausted retries (HTTP {last_status})")


@dataclass(frozen=True)
class SpAlbum:
    id: str
    artist: str          # first artist only — matches beets albumartist semantics
    name: str
    year: int
    total_tracks: int


@dataclass(frozen=True)
class SpTrack:
    id: str
    title: str
    artist: str
    duration_s: int
    album: SpAlbum


@dataclass(frozen=True)
class SpPlaylist:
    id: str
    name: str
    track_count: int


class SpotifyError(RuntimeError):
    pass


class SpotifyRateLimitError(SpotifyError):
    """Spotify HTTP 429 rate-limit error with optional Retry-After seconds."""

    def __init__(
        self,
        message: str = "Spotify rate limited",
        *,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.retry_after = retry_after


def _first_artist_name(raw_artists: Any) -> str:
    if not isinstance(raw_artists, list):
        return ""
    for a in raw_artists:
        if isinstance(a, dict):
            name = a.get("name")
            if isinstance(name, str) and name:
                return name
    return ""


def _album_from_json(a: dict | None) -> SpAlbum:
    if not isinstance(a, dict):
        return SpAlbum(id="", artist="", name="", year=0, total_tracks=0)
    try:
        total = int(a.get("total_tracks") or 0)
    except (TypeError, ValueError):
        total = 0
    return SpAlbum(
        id=str(a.get("id") or ""),
        artist=_first_artist_name(a.get("artists")),
        name=str(a.get("name") or ""),
        year=extract_year(a.get("release_date")),
        total_tracks=total,
    )


class SpotifyClient:
    _RL_ATTEMPTS = 8            # ride out a rolling window, don't give up early
    _RL_PACE = 2.0              # initial inter-call floor (seconds); adapts

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_file: Path,
        proxy: str | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = Path(token_file)
        if proxy is None:
            proxy_str = (os.environ.get("SPOTIFY_PROXY") or "").strip()
        else:
            proxy_str = proxy.strip()
        self._proxy = proxy_str
        self._http = HttpSession(
            timeout=HTTP_TIMEOUT,
            user_agent=default_user_agent(component="spotify"),
            proxy=proxy_str or None,
        )
        self._access_token = ""
        self._expires_at = 0.0
        self._last_call = 0.0
        self._pace = self._RL_PACE

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    @property
    def proxies(self) -> dict[str, str]:
        return dict(self._http.proxies)

    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def has_sp_dc(self) -> bool:
        return bool(self._state().get("sp_dc"))

    @staticmethod
    def _totp(server_ts: float) -> tuple[str, int]:
        """(6-digit code, version) from the current secret cipher, RFC-6238
        over 30s windows, keyed by Spotify's own server clock."""
        transformed = [e ^ ((t % 33) + 9) for t, e in enumerate(_TOTP_CIPHER)]
        key = "".join(str(n) for n in transformed).encode()
        counter = struct.pack(">Q", int(server_ts // 30))
        h = hmac.new(key, counter, hashlib.sha1).digest()
        off = h[-1] & 0x0F
        code = (struct.unpack(">I", h[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code:06d}", _TOTP_VER

    @staticmethod
    def _server_time(mint: HttpSession) -> float:
        """Spotify's clock (Date header) — TOTP must match their window."""
        try:
            resp = mint.request(
                "HEAD",
                "https://open.spotify.com/",
                headers={"User-Agent": WEB_UA},
                timeout=HTTP_TIMEOUT,
                raise_for_status=False,
            )
            dt = email.utils.parsedate_to_datetime(resp.headers["Date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return time.time()

    def _refresh_web(self, sp_dc: str) -> None:
        # Dedicated throwaway session so anonymous cookies from HEAD never
        # collide with our explicit sp_dc header.
        with HttpSession(
            timeout=HTTP_TIMEOUT,
            user_agent=WEB_UA,
            proxy=self._proxy or None,
        ) as mint:
            headers = {
                "Cookie": f"sp_dc={sp_dc}",
                "User-Agent": WEB_UA,
                "Accept": "application/json",
                "Referer": "https://open.spotify.com/",
                "App-Platform": "WebPlayer",
            }
            last = ""
            prev_ts = 0.0
            for idx, reason in enumerate(("transport", "init")):
                server_ts = self._server_time(mint)
                if idx > 0 and int(server_ts // 30) == int(prev_ts // 30):
                    # If the clock is still in the exact same 30s window after
                    # "transport" failed, advance to the next window on retry.
                    server_ts = server_ts + 30.0
                prev_ts = server_ts
                mint.clear_cookies()
                otp, ver = self._totp(server_ts)
                try:
                    resp = mint.get(
                        WEB_TOKEN_URL,
                        params={
                            "reason": reason,
                            "productType": "web-player",
                            "totp": otp,
                            "totpServer": otp,
                            "totpVer": ver,
                        },
                        headers=headers,
                        timeout=HTTP_TIMEOUT,
                        raise_for_status=False,
                    )
                except HttpError as exc:
                    raise SpotifyError(f"sp_dc token mint failed: {exc}") from exc

                if resp.status_code == 200:
                    try:
                        tok = resp.json()
                    except ValueError as exc:
                        raise SpotifyError(
                            f"sp_dc token mint returned invalid JSON: {exc}"
                        ) from exc
                    if isinstance(tok, dict) and tok.get("accessToken") and not tok.get("isAnonymous"):
                        self._access_token = str(tok["accessToken"])
                        expiry_ms = tok.get("accessTokenExpirationTimestampMs")
                        if expiry_ms:
                            try:
                                delta = max(60.0, (float(expiry_ms) / 1000.0) - time.time() - 60.0)
                            except (TypeError, ValueError):
                                delta = 1800.0
                            self._expires_at = time.monotonic() + delta
                        else:
                            self._expires_at = time.monotonic() + 1800.0
                        return
                    last = "anonymous token (cookie not logged in)"
                else:
                    last = f"{resp.status_code} {resp.text[:120]}"
            raise SpotifyError(
                f"sp_dc token mint failed: {last} — TOTP secret may have "
                "rotated or sp_dc expired; grab a fresh sp_dc while logged "
                "into open.spotify.com"
            )

    def _refresh(self) -> None:
        state = self._state()
        if state.get("sp_dc"):
            self._refresh_web(str(state["sp_dc"]))
            return
        if not state.get("refresh_token"):
            raise SpotifyError(
                "no sp_dc cookie or refresh token stored — connect Spotify in Settings"
            )
        try:
            resp = self._http.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": state["refresh_token"],
                },
                auth=(self.client_id, self.client_secret),
                timeout=HTTP_TIMEOUT,
                raise_for_status=False,
            )
        except HttpError as exc:
            raise SpotifyError(f"token refresh failed: {exc}") from exc
        if resp.status_code != 200:
            raise SpotifyError(
                f"token refresh failed: {resp.status_code} {resp.text[:200]}"
            )
        try:
            tok = resp.json()
        except ValueError as exc:
            raise SpotifyError(f"token refresh invalid JSON: {exc}") from exc
        if not isinstance(tok, dict) or not tok.get("access_token"):
            raise SpotifyError("token refresh response missing access_token")
        self._access_token = str(tok["access_token"])
        self._expires_at = time.monotonic() + max(
            60, int(tok.get("expires_in") or 3600) - 60
        )
        # Merge-write atomically so sp_dc and refresh_token never clobber each other.
        new_rt = tok.get("refresh_token")
        if new_rt and new_rt != state["refresh_token"]:
            config.save_spotify_token(self.token_file, {"refresh_token": new_rt})

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return a valid bearer token, refreshing when missing, expired, or forced."""
        if force_refresh:
            self._access_token = ""
            self._expires_at = 0.0
        if not self._access_token or time.monotonic() >= self._expires_at:
            self._refresh()
        return self._access_token

    def me(self) -> dict:
        """Profile of the authenticated user — the auth smoke test."""
        return self._get("/me")

    def playlist_info(self, playlist_id: str) -> SpPlaylist:
        pid_q = urllib.parse.quote(str(playlist_id), safe="")
        p = self._get(f"/playlists/{pid_q}", {"fields": "id,name,tracks(total)"})
        pid = str(p.get("id") or playlist_id)
        try:
            total = int((p.get("tracks") or {}).get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        return SpPlaylist(
            id=pid,
            name=str(p.get("name") or pid),
            track_count=total,
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        full_url = url if url.startswith("http") else API + url
        resp = paced_spotify_call(
            label=f"GET {url}",
            attempts=self._RL_ATTEMPTS,
            pace_floor=self._RL_PACE,
            get_pace=lambda: self._pace,
            set_pace=lambda v: setattr(self, "_pace", v),
            get_last_call=lambda: self._last_call,
            set_last_call=lambda v: setattr(self, "_last_call", v),
            get_token=self.get_access_token,
            do_request=lambda token: self._http.get(
                full_url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
                raise_for_status=False,
            ),
        )
        try:
            data = resp.json()
        except ValueError as exc:
            raise SpotifyError(f"GET {url}: invalid JSON ({exc})") from exc
        if not isinstance(data, dict):
            raise SpotifyError(f"GET {url}: expected JSON object")
        return data

    def _paginate(self, url: str, params: dict | None = None) -> list[dict]:
        items: list[dict] = []
        page = self._get(url, params)
        pages = 1
        while True:
            raw_items = page.get("items") or []
            if isinstance(raw_items, list):
                items.extend(x for x in raw_items if isinstance(x, dict))
            nxt = page.get("next")
            if not nxt:
                break
            if pages >= MAX_PAGES:
                log.warning(
                    "pagination cap hit at %d items for %s — remainder silently unfetched",
                    len(items),
                    url,
                )
                break
            page = self._get(nxt)
            pages += 1
        return items

    def saved_albums(self) -> list[SpAlbum]:
        raw = self._paginate("/me/albums", {"limit": 50})
        return [
            _album_from_json(e.get("album"))
            for e in raw
            if isinstance(e.get("album"), dict)
        ]

    def playlists(self) -> list[SpPlaylist]:
        raw = self._paginate("/me/playlists", {"limit": 50})
        return [
            SpPlaylist(
                id=str(p["id"]),
                name=str(p.get("name") or p["id"]),
                track_count=int((p.get("tracks") or {}).get("total") or 0),
            )
            for p in raw
            if p.get("id")
        ]

    def album(self, album_id: str) -> SpAlbum:
        aid_q = urllib.parse.quote(str(album_id), safe="")
        return _album_from_json(self._get(f"/albums/{aid_q}"))

    def track_album(self, track_id: str) -> SpAlbum:
        tid_q = urllib.parse.quote(str(track_id), safe="")
        return _album_from_json(self._get(f"/tracks/{tid_q}").get("album"))

    def playlist_tracks(self, playlist_id: str) -> list[SpTrack]:
        pid_q = urllib.parse.quote(str(playlist_id), safe="")
        raw = self._paginate(
            f"/playlists/{pid_q}/tracks",
            {
                "limit": 50,
                "fields": (
                    "items(track(id,name,duration_ms,artists(name),"
                    "album(id,name,release_date,total_tracks,artists(name)))),next"
                ),
            },
        )
        tracks: list[SpTrack] = []
        for e in raw:
            t = e.get("track")
            if not isinstance(t, dict) or not t.get("id"):
                continue
            tracks.append(SpTrack(
                id=str(t["id"]),
                title=str(t.get("name") or ""),
                artist=_first_artist_name(t.get("artists")),
                duration_s=int(t.get("duration_ms") or 0) // 1000,
                album=_album_from_json(t.get("album")),
            ))
        return tracks

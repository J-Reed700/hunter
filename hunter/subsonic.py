"""Minimal Subsonic / Navidrome REST client (Layer 3).

Speaks Subsonic API v1.16.1 using salted MD5 auth tokens and POST form bodies
for playlist writes so large playlists never hit HTTP 414. Error messages never
embed raw query strings or credentials.
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from hunter.http_client import HttpError, HttpSession, default_user_agent

HTTP_TIMEOUT = 30


class SubsonicError(RuntimeError):
    """Subsonic transport or API error with optional structured Subsonic ``code``."""

    def __init__(self, message: str, *, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


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
            detail = f"HTTP {exc.status}" if exc.status is not None else type(exc).__name__
            raise SubsonicError(
                f"subsonic {endpoint}: {detail}",
                status=exc.status,
            ) from None
        if not isinstance(data, dict):
            raise SubsonicError(f"subsonic {endpoint}: invalid response")
        body = data.get("subsonic-response")
        if not isinstance(body, dict):
            raise SubsonicError(f"subsonic {endpoint}: missing subsonic-response")
        if body.get("status") != "ok":
            err = body.get("error")
            code: int | None = None
            if isinstance(err, dict):
                raw_code = err.get("code")
                if isinstance(raw_code, int) and not isinstance(raw_code, bool):
                    code = raw_code
                msg = err.get("message") or str(err)
            else:
                msg = str(err)
            raise SubsonicError(f"subsonic {endpoint}: {msg}", code=code)
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


__all__ = ["HTTP_TIMEOUT", "SubsonicClient", "SubsonicError"]

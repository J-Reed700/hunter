"""Prowlarr fallback — torrent/usenet when Soulseek keeps coming up empty.

Soulseek is the primary channel (peer-to-peer, lossless-rich, no indexer
politics). But a want that has missed on slskd N times in a row is usually
obscure or old enough that a tracker or usenet indexer still has it. This
asks Prowlarr — which already fronts every indexer in the stack — for the
album, ranks releases the same way hunter ranks peers (lossless first,
reliability next), and hands the winner to Prowlarr's grab endpoint.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from hunter.domain import (
    extract_match_tokens,
    norm_album,
    norm_artist,
    normalize,
    tokens_match_path,
)
from hunter.http_client import HttpError, HttpSession, default_user_agent

log = logging.getLogger("hunter.prowlarr")

_AUDIO_CATEGORY = 3000
_LOSSLESS_CATEGORY = 3040
_LOSSY_RE = re.compile(
    r"\b(?:mp3|aac|ogg|opus|wma|m4a|320\s*k(?:bps)?|256\s*k(?:bps)?|"
    r"192\s*k(?:bps)?|128\s*k(?:bps)?|cbr|vbr|v0|v2|transcode|from\s+flac)\b",
    re.I,
)
_LOSSLESS_RE = re.compile(
    r"\b(flac|alac|ape|wav|aiff|wv|dsf|lossless|24[ -]?bit|16[ -]?bit|dsd)\b", re.I
)
_DISCOGRAPHY_RE = re.compile(
    r"\b(discograph|complete works|anthology|box ?set|collection of)\w*", re.I
)
_MIN_BYTES = 3 * 1024 * 1024           # filter spam/sub-3MB junk (allows EPs/singles & size==0)
_MAX_BYTES = 6 * 1024 * 1024 * 1024    # a 6 GB release is a discography


def is_prowlarr_key(username: str | None) -> bool:
    """Return True when a want's ``grabbed_username`` originated from Prowlarr."""
    return bool(username and str(username).startswith("prowlarr:"))


class ProwlarrError(RuntimeError):
    pass


@dataclass(frozen=True)
class Release:
    title: str
    guid: str
    indexer_id: int
    indexer: str
    protocol: str            # "torrent" | "usenet"
    size: int
    seeders: int | None
    lossless: bool
    age_hours: float

    @property
    def key(self) -> tuple[str, str]:
        """(username, dir) shape so the per-want tried-list and the grab
        record work unchanged."""
        return f"prowlarr:{self.indexer}", self.title


class ProwlarrClient:
    def __init__(self, url: str, api_key: str, timeout: int = 30):
        self._url = url.rstrip("/")
        self._timeout = int(timeout)
        self._http = HttpSession(
            timeout=self._timeout,
            user_agent=default_user_agent(component="prowlarr"),
            headers={"X-Api-Key": api_key},
        )

    def __enter__(self) -> "ProwlarrClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def search(self, query: str, timeout: int | None = None) -> list[Release]:
        try:
            rows = self._http.get_json(
                f"{self._url}/api/v1/search",
                params={
                    "query": query,
                    "categories": _AUDIO_CATEGORY,
                    "type": "search",
                    "limit": 60,
                },
                timeout=timeout or self._timeout,
            )
        except HttpError as exc:
            raise ProwlarrError(f"search failed: {exc}") from exc
        if not isinstance(rows, list):
            raise ProwlarrError(f"search failed: expected list, got {type(rows).__name__}")
        out: list[Release] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                out.append(_parse(row))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def grab(self, release: Release) -> None:
        """Ask Prowlarr to push the release to its download client."""
        try:
            self._http.post(
                f"{self._url}/api/v1/search",
                json={"guid": release.guid, "indexerId": release.indexer_id},
                timeout=self._timeout,
            )
        except HttpError as exc:
            raise ProwlarrError(f"grab failed: {exc}") from exc


def _flatten_categories(cats: Any) -> set[int]:
    ids: set[int] = set()
    stack = list(cats or [])
    while stack:
        c = stack.pop()
        if isinstance(c, dict):
            if isinstance(c.get("id"), int):
                ids.add(c["id"])
            stack.extend(c.get("subCategories") or [])
    return ids


def _parse(row: dict) -> Release:
    raw_guid = row.get("guid")
    if raw_guid is None or not str(raw_guid).strip():
        raise ValueError("missing guid")
    cats = _flatten_categories(row.get("categories"))
    title = str(row.get("title") or "")
    is_lossy_tag = bool(_LOSSY_RE.search(title))
    lossless = (not is_lossy_tag) and (
        (_LOSSLESS_CATEGORY in cats) or bool(_LOSSLESS_RE.search(title))
    )
    proto = str(row.get("protocol") or "torrent").strip().lower() or "torrent"
    raw_seeders = row.get("seeders")
    seeders = raw_seeders if (isinstance(raw_seeders, int) and not isinstance(raw_seeders, bool)) else None
    return Release(
        title=title,
        guid=str(raw_guid),
        indexer_id=int(row["indexerId"]),
        indexer=str(row.get("indexer") or row["indexerId"]),
        protocol=proto,
        size=int(row.get("size") or 0),
        seeders=seeders,
        lossless=lossless,
        age_hours=float(row.get("ageHours") or 0.0),
    )


def rank(
    releases: list[Release],
    artist: str,
    album: str,
    *,
    prefer_lossless: bool,
    lossless_only: bool,
    tried: set[tuple[str, str]],
) -> list[Release]:
    """Filter to releases that are plausibly THIS album, then order them.

    Uses ``domain.extract_match_tokens`` + ``domain.tokens_match_path`` with
    ``require_all=True`` so short names (U2, R.E.M., The XX) match safely and
    substring false positives (``air`` in ``chair``) are rejected. Fails closed
    if artist or album tokens are empty.
    """
    a_tokens = extract_match_tokens(norm_artist(artist))
    b_tokens = extract_match_tokens(norm_album(album))
    if not a_tokens or not b_tokens:
        return []
    want_disco = bool(_DISCOGRAPHY_RE.search(album or ""))
    keep: list[Release] = []
    for r in releases:
        if r.key in tried:
            continue
        if lossless_only and not r.lossless:
            continue
        if r.protocol == "torrent" and r.seeders is not None and r.seeders <= 0:
            continue
        if r.size < 0 or (0 < r.size < _MIN_BYTES) or r.size > _MAX_BYTES:
            continue
        if _DISCOGRAPHY_RE.search(r.title) and not want_disco:
            continue
        hay = normalize(r.title)
        if not tokens_match_path(a_tokens, hay, require_all=True):
            continue
        if not tokens_match_path(b_tokens, hay, require_all=True):
            continue
        keep.append(r)
    keep.sort(key=lambda r: (
        -(1 if (r.lossless and prefer_lossless) else 0),
        0 if r.protocol == "usenet" else 1,          # usenet: no seeder roulette
        -(r.seeders or 0),
        r.age_hours,
    ))
    return keep

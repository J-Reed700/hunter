"""slskd REST client + candidate ranking.

Drives slskd's API directly (same surface soularr scraped, minus the
Lidarr middleman). Searches are bounded: we poll until slskd marks the
search complete or our monotonic timeout hits, then delete the search
server-side so slskd's search list doesn't accumulate garbage.

Candidate = one (username, remote-dir) whose audio files look like the
album we want. Ranking prefers lossless (when configured), then closeness
to the expected track count, quality tier, reputation, free slot, queue
length, and upload speed.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

from hunter.domain import AUDIO_EXTENSIONS, LOSSLESS_EXTENSIONS, dir_leaf
from hunter.http_client import HttpError, HttpSession, default_user_agent

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 30
POLL_INTERVAL = 3


def _safe_int(val: Any, default: int = 0) -> int:
    if val is None or isinstance(val, bool):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None or isinstance(val, bool):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Candidate:
    username: str
    dir: str
    files: tuple[dict, ...]     # slskd file dicts: {"filename": ..., "size": ...}
    lossless: bool
    audio_count: int
    has_free_slot: bool
    queue_length: int
    upload_speed: int
    avg_bitrate: int            # kbps; 0 = unknown


def candidate_to_dict(
    c: Candidate,
    *,
    max_files: int = 80,
    tried: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Serialize a ``Candidate`` for JSON API responses."""
    out: dict[str, Any] = {
        "username": c.username,
        "dir": c.dir,
        "dir_leaf": dir_leaf(c.dir),
        "count": c.audio_count,
        "lossless": c.lossless,
        "bitrate": c.avg_bitrate,
        "queue": c.queue_length,
        "slot": c.has_free_slot,
        "size": sum(_safe_int(f.get("size")) for f in c.files),
        "files": [
            {"filename": f.get("filename"), "size": f.get("size")}
            for f in c.files[:max_files]
        ],
    }
    if tried is not None:
        norm_tried = {(u, norm_dir(d)) for u, d in tried}
        out["tried"] = (c.username, norm_dir(c.dir)) in norm_tried
    return out


def _avg_bitrate(files: list[dict]) -> tuple[int, bool]:
    """(kbps, reported). Peer-reported bitRate when at least half of files
    report it; else size/length over files with length > 0 (reported=True
    when at least half have length > 0); else a 240s-per-track size estimate
    (reported=False). Zero-byte dirs return (0, False)."""
    if not files:
        return 0, False
    total_size = sum(_safe_int(f.get("size")) for f in files)
    if total_size <= 0:
        return 0, False

    need = (len(files) + 1) // 2
    reported_br = [
        br for f in files if (br := _safe_int(f.get("bitRate"))) > 0
    ]
    if len(reported_br) >= need:
        return int(sum(reported_br) / len(reported_br)), True

    with_len = [
        (f, ln) for f in files if (ln := _safe_float(f.get("length"))) > 0
    ]
    if with_len:
        bits = sum(_safe_int(f.get("size")) for f, _ in with_len) * 8
        secs = sum(ln for _, ln in with_len)
        kbps = int(bits / secs / 1000) if secs > 0 else 0
        return kbps, len(with_len) >= need

    if reported_br:
        return int(sum(reported_br) / len(reported_br)), False

    est_secs = len(files) * 240
    kbps = int((total_size * 8) / est_secs / 1000) if est_secs else 0
    return kbps, False


class SlskdError(RuntimeError):
    """`status` is the HTTP status when slskd answered (4xx = our request was
    bad, e.g. a query with no token slskd will search on); None when the
    transport itself failed (connection refused, timeout)."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def _ext(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def norm_dir(path: str) -> str:
    norm = (path or "").replace("\\", "/")
    if norm == "/":
        return "/"
    return norm.rstrip("/")


def _dir_of(filename: str) -> str:
    # Soulseek paths are windows-style more often than not; handle both.
    norm = (filename or "").replace("\\", "/")
    return norm_dir(norm.rsplit("/", 1)[0]) if "/" in norm else ""


class SlskdClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = HTTP_TIMEOUT):
        self.base = base_url.rstrip("/") + "/api/v0"
        self._http = HttpSession(
            timeout=timeout,
            user_agent=default_user_agent(component="slskd"),
            headers={"X-API-Key": api_key},
        )

    def __enter__(self) -> "SlskdClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _req(self, method: str, path: str, **kw: Any):
        try:
            return self._http.request(method, self.base + path, **kw)
        except HttpError as exc:
            raise SlskdError(f"{method} {path}: {exc}", status=exc.status) from exc

    def _req_json(self, method: str, path: str, **kw: Any) -> Any:
        try:
            return self._http.request_json(method, self.base + path, **kw)
        except HttpError as exc:
            raise SlskdError(f"{method} {path}: {exc}", status=exc.status) from exc

    def search(self, text: str, timeout: int) -> list[dict]:
        """Run a search to completion (or monotonic timeout); return response list."""
        timeout_s = max(1, int(timeout))
        created = self._req_json(
            "POST",
            "/searches",
            json={"searchText": text, "searchTimeout": timeout_s * 1000},
        )
        if not isinstance(created, dict) or not created.get("id"):
            raise SlskdError("POST /searches: missing search id")
        search_id = urllib.parse.quote(str(created["id"]), safe="")
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    break
                time.sleep(min(POLL_INTERVAL, rem))
                try:
                    st = self._req_json("GET", f"/searches/{search_id}")
                except SlskdError as exc:
                    if exc.status is not None and 400 <= exc.status < 500:
                        raise
                    log.debug("search poll transient error for %s: %s", search_id, exc)
                    continue
                if isinstance(st, dict) and st.get("isComplete"):
                    break
            data = self._req_json("GET", f"/searches/{search_id}/responses")
            if data is None:
                return []
            if not isinstance(data, list):
                raise SlskdError(f"GET /searches/{search_id}/responses: expected list")
            return data
        finally:
            try:
                self._req("DELETE", f"/searches/{search_id}")
            except SlskdError as exc:      # cleanup is best-effort
                log.debug("search cleanup failed: %s", exc)

    def enqueue(self, username: str, files: list[dict]) -> None:
        payload = [
            {"filename": f["filename"], "size": f["size"]}
            for f in files
            if isinstance(f, dict) and f.get("filename") and f.get("size") is not None
        ]
        if not payload:
            raise SlskdError(f"enqueue {username}: no enqueueable files")
        user_q = urllib.parse.quote(username, safe="")
        self._req("POST", f"/transfers/downloads/{user_q}", json=payload)

    def _iter_download_files(self, *, username_filter: str | None = None):
        """Yield ``(username, normalized_dir, file_dict)`` across all downloads."""
        for user in self.downloads():
            if not isinstance(user, dict):
                continue
            username = user.get("username") or ""
            if username_filter is not None and username != username_filter:
                continue
            for d in user.get("directories") or []:
                if not isinstance(d, dict):
                    continue
                dir_fallback = norm_dir(d.get("directory") or "")
                for f in d.get("files") or []:
                    if not isinstance(f, dict):
                        continue
                    fdir = _dir_of(f.get("filename", "")) or dir_fallback
                    yield username, norm_dir(fdir), f

    def cancel_dir(self, username: str, dir_: str) -> int:
        """Cancel+remove every non-succeeded download from (peer, dir).
        Best-effort; returns how many transfers were removed."""
        removed = 0
        target_dir = norm_dir(dir_)
        user_q = urllib.parse.quote(username, safe="")
        for _user, fdir, f in self._iter_download_files(username_filter=username):
            if fdir != target_dir:
                continue
            state = f.get("state") or ""
            if "Succeeded" in state:
                continue
            tid = f.get("id")
            if tid is None or str(tid) == "":
                continue
            tid_q = urllib.parse.quote(str(tid), safe="")
            try:
                self._req(
                    "DELETE",
                    f"/transfers/downloads/{user_q}/{tid_q}?remove=true",
                )
                removed += 1
            except SlskdError as exc:
                log.debug("cancel failed for %s: %s", f.get("filename"), exc)
        return removed

    def downloads(self) -> list[dict]:
        """All current downloads, slskd's per-user nested shape."""
        data = self._req_json("GET", "/transfers/downloads")
        if data is None:
            return []
        if not isinstance(data, list):
            raise SlskdError("GET /transfers/downloads: expected list")
        return data

    def download_states(self) -> dict[tuple[str, str], dict]:
        """Flatten downloads() into {(username, remote_dir): summary}.

        summary: {"total", "succeeded", "errored", "active", "pct", "state"}
        where ``state`` distinguishes terminal states cleanly:
        ``"active"``, ``"succeeded"``, ``"failed"`` (errored > 0, succeeded == 0),
        and ``"partial_error"`` (active == 0, errored > 0, succeeded > 0).
        """
        out: dict[tuple[str, str], dict] = {}
        for username, fdir, f in self._iter_download_files():
            key = (username, fdir)
            s = out.setdefault(
                key,
                {"total": 0, "succeeded": 0, "errored": 0, "active": 0, "pct": 0.0},
            )
            s["total"] += 1
            state = f.get("state") or ""
            if "Succeeded" in state:
                s["succeeded"] += 1
                s["pct"] += 100.0
            elif "Completed" in state:   # Errored/Cancelled/TimedOut/…
                s["errored"] += 1
            else:
                s["active"] += 1
                pct = _safe_float(f.get("percentComplete"))
                s["pct"] += max(0.0, min(100.0, pct))
        for s in out.values():
            s["pct"] = round(s["pct"] / s["total"], 1) if s["total"] else 0.0
            if s["active"] > 0:
                s["state"] = "active"
            elif s["errored"] > 0 and s["succeeded"] == 0:
                s["state"] = "failed"
            elif s["errored"] > 0 and s["succeeded"] > 0:
                s["state"] = "partial_error"
            elif s["succeeded"] > 0:
                s["state"] = "succeeded"
            else:
                s["state"] = "unknown"
        return out


def rank_candidates(
    responses: list[dict],
    expected_tracks: int | None,
    min_ratio: float,
    max_ratio: float,
    prefer_lossless: bool,
    exclude: set[tuple[str, str]],
    min_bitrate: int = 0,
    banned: set[str] | None = None,
    min_files: int = 3,
    reputation: dict[str, float] | None = None,
) -> list[Candidate]:
    """Group each peer's files by remote dir, filter to album-shaped dirs,
    return best-first."""
    norm_exclude = {(u, norm_dir(d)) for u, d in exclude}
    has_expected = bool(expected_tracks and expected_tracks > 0)
    candidates: list[Candidate] = []
    for resp in responses:
        if not isinstance(resp, dict):
            continue
        username = resp.get("username") or ""
        if not username or (banned and username in banned):
            continue
        by_dir: dict[str, list[dict]] = {}
        for f in resp.get("files") or []:
            if not isinstance(f, dict):
                continue
            fname = f.get("filename") or ""
            if _ext(fname) in AUDIO_EXTENSIONS:
                by_dir.setdefault(_dir_of(fname), []).append(f)
        for dir_, files in by_dir.items():
            if (username, norm_dir(dir_)) in norm_exclude:
                continue
            n = len(files)
            if has_expected and expected_tracks is not None:
                min_n = 1 if expected_tracks == 1 else max(2, int(expected_tracks * min_ratio))
                max_n = max(min_n, int(expected_tracks * max_ratio))
                if n < min_n or n > max_n:
                    continue
            elif not min_files <= n <= 60:   # unknown size: sane album shape
                continue                     # (browse passes min_files=1)
            if any(_safe_int(f.get("size")) <= 0 for f in files):
                continue                     # reject 0-byte corrupt files (lossy or lossless)
            lossless = all(_ext(f.get("filename") or "") in LOSSLESS_EXTENSIONS for f in files)
            bitrate, reported = _avg_bitrate(files)
            if min_bitrate and not lossless:
                if bitrate <= 0 or (reported and bitrate < min_bitrate):
                    continue
            candidates.append(Candidate(
                username=username,
                dir=dir_,
                files=tuple(files),
                lossless=lossless,
                audio_count=n,
                has_free_slot=bool(resp.get("hasFreeUploadSlot")),
                queue_length=_safe_int(resp.get("queueLength")),
                upload_speed=_safe_int(resp.get("uploadSpeed")),
                avg_bitrate=bitrate,
            ))

    rep = reputation or {}

    def sort_key(c: Candidate):
        if prefer_lossless:
            lossless_tier = 0 if c.lossless else 1
            quality = -32 if c.lossless else -(min(c.avg_bitrate, 1999) // 64)
        else:
            # Neutral lossless tier and capped bitrate bucket so lossless
            # never secretly outranks 320kbps lossy when prefer_lossless=False.
            lossless_tier = 0
            quality = -(min(c.avg_bitrate, 320) // 64)
        rscore = rep.get(c.username, 0.0)
        rep_tier = 0 if rscore > 0.25 else (2 if rscore < -0.25 else 1)
        return (
            lossless_tier,
            abs(c.audio_count - expected_tracks) if (has_expected and expected_tracks) else 0,
            quality,
            rep_tier,
            0 if c.has_free_slot else 1,
            c.queue_length,
            -c.upload_speed,
        )

    return sorted(candidates, key=sort_key)

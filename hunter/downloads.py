"""Download bundles — take music out of the library as files.

A *bundle* is a ZIP built in the background from a selection (track ids,
album ids, artist names — any mix). The registry lives in
``<state_dir>/downloads/index.json``; the archives in ``<state_dir>/downloads/<id>.zip``.
The UI's Downloads view polls the registry for progress and serves the
finished archive with HTTP Range support (server.py), so a 20 GB discography
resumes after a dropped connection.

Rules:
* Audio is already compressed — archives are STORED (no deflate), so build
  speed is disk-read speed and the zip is exactly the sum of its files.
* Never read the beets DB rw, never touch media. Paths come from
  ``libquery`` (ro) and are mapped through ``host_path`` onto the ro mount.
* Bounded: per-bundle size cap, total on-disk cap (oldest ready bundles are
  evicted), expiry (default 24 h), file-count cap. One builder thread.
* Single tracks are not bundled — ``/api/dl/track`` streams the file itself.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path

from hunter import domain, libquery
from hunter.config import HunterConfig

log = logging.getLogger(__name__)

MAX_BUNDLE_BYTES = 30 * (1 << 30)
KEEP_BYTES = 60 * (1 << 30)
TTL_S = 24 * 3600
MAX_FILES = 20_000
MIN_FREE_HEADROOM_BYTES = 2 * (1 << 30)
MAX_ACTIVE_PER_USER = 2
VIEWER_MAX_PENDING_BYTES = 10 * (1 << 30)
VIEWER_MAX_TOTAL_BYTES = 20 * (1 << 30)

_lock = threading.RLock()
_worker: threading.Thread | None = None
_wake = threading.Event()
_cancel: set[str] = set()
_cfg: HunterConfig | None = None

_SAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


def _limits(cfg: HunterConfig) -> tuple[int, int, int, int]:
    return (
        int(getattr(cfg, "dl_max_bundle_bytes", MAX_BUNDLE_BYTES)),
        int(getattr(cfg, "dl_keep_bytes", KEEP_BYTES)),
        int(getattr(cfg, "dl_ttl_seconds", TTL_S)),
        int(getattr(cfg, "dl_max_files", MAX_FILES)),
    )


def _dir(cfg: HunterConfig) -> Path:
    d = cfg.state_dir / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _index_path(cfg: HunterConfig) -> Path:
    return _dir(cfg) / "index.json"


def _load(cfg: HunterConfig) -> list[dict]:
    try:
        with open(_index_path(cfg), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(cfg: HunterConfig, items: list[dict]) -> None:
    domain.atomic_write_json(_index_path(cfg), items, indent=1)


def _safe(name: str) -> str:
    return (_SAFE.sub("_", name).strip(" .") or "_")[:120]


def _unique_arc(arc: str, used: set[str]) -> str:
    key = arc.casefold()
    if key not in used:
        used.add(key)
        return arc
    p = Path(arc)
    stem = str(p.with_suffix(""))
    ext = p.suffix
    n = 2
    while True:
        cand = f"{stem} ({n}){ext}"
        if cand.casefold() not in used:
            used.add(cand.casefold())
            return cand
        n += 1


# --------------------------------------------------------------------------
# selection → files
# --------------------------------------------------------------------------

def resolve(cfg: HunterConfig, *, item_ids: list[int] = (), album_ids: list[int] = (),
            artists: list[str] = ()) -> dict:
    """Files for a selection, with the archive path each should get
    (``Artist/Album (Year)/NN - Title.ext``, mirroring beets' layout)."""
    _, _, _, max_files = _limits(cfg)
    conn = libquery.connect(cfg)
    files: dict[int, dict] = {}
    try:
        sql = ("SELECT i.id, i.path, i.title, i.artist, i.albumartist, a.album, a.year, i.track, i.disc,"
               " COALESCE(MAX(i.disctotal),1) AS disctotal, i.format, i.album_id, a.artpath"
               " FROM items i LEFT JOIN albums a ON a.id = i.album_id WHERE %s GROUP BY i.id")
        chunks = []
        ids = [int(x) for x in item_ids][:max_files]
        for part in domain.chunked(ids, 400):
            chunks += conn.execute(sql % f"i.id IN ({','.join('?' * len(part))})", part).fetchall()
        aids = [int(x) for x in album_ids][:5000]
        for part in domain.chunked(aids, 400):
            chunks += conn.execute(sql % f"i.album_id IN ({','.join('?' * len(part))})", part).fetchall()
        artist_list = [a for a in dict.fromkeys(artists) if a][:500]
        for part in domain.chunked(artist_list, 400):
            chunks += conn.execute(sql % f"i.albumartist IN ({','.join('?' * len(part))})", part).fetchall()
        arts: dict[int, tuple[str, str]] = {}
        for r in chunks:
            if r["id"] in files:
                continue
            hp = domain.container_to_host_path(cfg, r["path"])
            if not hp:
                continue
            artist = _safe(r["albumartist"] or r["artist"] or "Unknown Artist")
            album = _safe((r["album"] or "Unknown Album") + (f" ({r['year']})" if r["year"] else ""))
            ext = hp.suffix or ("." + (r["format"] or "bin").lower())
            track_num = int(r["track"] or 0)
            num = f"{int(r['disc']):d}-{track_num:02d}" if (r["disctotal"] or 1) > 1 and r["disc"] else f"{track_num:02d}"
            arc = f"{artist}/{album}/{num} - {_safe(r['title'] or hp.stem)}{ext}"
            files[r["id"]] = {"id": r["id"], "src": str(hp), "arc": arc}
            if r["artpath"] and r["album_id"] not in arts:
                ap = domain.container_to_host_path(cfg, r["artpath"])
                if ap:
                    arts[r["album_id"]] = (str(ap), f"{artist}/{album}/{_safe(ap.name)}")
    finally:
        conn.close()
    out = list(files.values())
    for aid, (src, arc) in arts.items():
        out.append({"id": -aid, "src": src, "arc": arc})
    total = 0
    missing = 0
    for f in out:
        try:
            f["bytes"] = os.path.getsize(f["src"])
            total += f["bytes"]
        except OSError:
            f["bytes"] = 0
            missing += 1
    return {"files": out, "bytes": total, "count": len(out) - missing, "missing": missing}


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

def summary(cfg: HunterConfig) -> dict:
    max_bundle_bytes, keep_bytes, ttl_s, _ = _limits(cfg)
    with _lock:
        items = _load(cfg)
    used = sum(b.get("bytes_done", 0) for b in items if b.get("status") in ("ready", "building"))
    return {"total": len(items), "building": sum(1 for b in items if b.get("status") in ("queued", "building")),
            "ready": sum(1 for b in items if b.get("status") == "ready"), "used_bytes": used,
            "keep_bytes": keep_bytes, "ttl_hours": ttl_s // 3600, "max_bundle_bytes": max_bundle_bytes}


def list_bundles(cfg: HunterConfig) -> list[dict]:
    with _lock:
        items = _load(cfg)
    return sorted(items, key=lambda b: b.get("created_ts", 0), reverse=True)


def get(cfg: HunterConfig, bid: str) -> dict | None:
    with _lock:
        for b in _load(cfg):
            if b["id"] == bid:
                return b
    return None


def create(cfg: HunterConfig, *, title: str, requested_by: str, item_ids=(), album_ids=(),
           artists=(), role: str = "") -> dict:
    max_bundle_bytes, _keep_bytes, ttl_s, max_files = _limits(cfg)
    sel = resolve(cfg, item_ids=item_ids, album_ids=album_ids, artists=artists)
    if not sel["count"]:
        return {"ok": False, "error": "nothing to download (no files found on disk)"}
    if sel["bytes"] > max_bundle_bytes:
        return {"ok": False, "error": f"bundle would be {sel['bytes'] / (1 << 30):.1f} GB — cap is "
                                      f"{max_bundle_bytes / (1 << 30):.0f} GB; split the selection"}
    if len(sel["files"]) > max_files:
        return {"ok": False, "error": f"too many files ({len(sel['files'])} > {max_files})"}
    dl_dir = _dir(cfg)
    bid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    now = int(time.time())
    b = {"id": bid, "title": (title or "download")[:160], "requested_by": requested_by,
         "created_ts": now, "status": "queued", "files": len(sel["files"]),
         "missing": sel["missing"], "bytes_total": sel["bytes"], "bytes_done": 0,
         "files_done": 0, "files_skipped": 0,
         "path": str(dl_dir / f"{bid}.zip"), "error": "", "expires_ts": now + ttl_s,
         "served": 0, "selection": {"item_ids": list(item_ids)[:200], "album_ids": list(album_ids)[:200],
                                    "artists": list(artists)[:50]}}
    with _lock:
        items = _load(cfg)
        try:
            stv = os.statvfs(dl_dir)
            free_bytes = stv.f_bavail * stv.f_frsize
        except OSError:
            free_bytes = None
        if free_bytes is not None:
            pending_remaining = sum(
                max(0, int(x.get("bytes_total", 0) or 0) - int(x.get("bytes_done", 0) or 0))
                for x in items
                if x.get("status") in ("queued", "building")
            )
            if free_bytes - pending_remaining - sel["bytes"] < MIN_FREE_HEADROOM_BYTES:
                return {"ok": False, "error": "insufficient free space in state directory (need 2 GB headroom after pending bundles)"}

        user_active = [x for x in items
                       if x.get("requested_by") == requested_by
                       and x.get("status") in ("queued", "building")]
        if len(user_active) >= MAX_ACTIVE_PER_USER:
            return {"ok": False,
                    "error": f"too many active bundles ({len(user_active)} >= {MAX_ACTIVE_PER_USER})"}
        if role == "viewer":
            pending_bytes = sum(int(x.get("bytes_total", 0) or 0) for x in user_active) + sel["bytes"]
            if pending_bytes > VIEWER_MAX_PENDING_BYTES:
                return {"ok": False,
                        "error": "viewer pending bundle quota exceeded (10 GB max)"}
            viewer_total = sum(
                int(x.get("bytes_total", 0) or 0)
                for x in items
                if x.get("requested_by") == requested_by
                and x.get("status") in ("queued", "building", "ready")
            ) + sel["bytes"]
            if viewer_total > VIEWER_MAX_TOTAL_BYTES:
                return {"ok": False,
                        "error": "viewer total bundle disk quota exceeded (20 GB max)"}
        items.append(b)
        _save(cfg, items)
        _write_manifest(cfg, bid, sel["files"])
        _ensure_worker(cfg)
        _wake.set()
    return {"ok": True, "bundle": b}


def _write_manifest(cfg: HunterConfig, bid: str, files: list[dict]) -> None:
    domain.atomic_write_json(_dir(cfg) / f"{bid}.files.json", files, indent=None)


def recover_on_startup(cfg: HunterConfig) -> dict:
    """Recover bundle registry & temp files after an unclean shutdown."""
    dl_dir = _dir(cfg)
    recovered_failed = 0
    removed_files = 0
    with _lock:
        items = _load(cfg)
        for b in items:
            if b.get("status") == "building":
                b["status"] = "failed"
                b["error"] = "interrupted by restart"
                recovered_failed += 1
        if recovered_failed:
            _save(cfg, items)
        known_ids = {b["id"] for b in items if b.get("id")}
        queued_ids = {b["id"] for b in items if b.get("status") == "queued" and b.get("id")}
        for p in list(dl_dir.iterdir()):
            name = p.name
            if name == "index.json":
                continue
            if name.endswith(".zip.part"):
                try:
                    p.unlink()
                    removed_files += 1
                except OSError:
                    pass
            elif name.endswith(".zip"):
                bid = name[:-4]
                if bid not in known_ids:
                    try:
                        p.unlink()
                        removed_files += 1
                    except OSError:
                        pass
            elif name.endswith(".files.json"):
                bid = name[:-11]
                if bid not in queued_ids:
                    try:
                        p.unlink()
                        removed_files += 1
                    except OSError:
                        pass
        if queued_ids:
            _ensure_worker(cfg)
            _wake.set()
    return {"failed": recovered_failed, "removed": removed_files, "queued": len(queued_ids)}


def cancel(cfg: HunterConfig, bid: str) -> dict:
    with _lock:
        items = _load(cfg)
        b = next((x for x in items if x["id"] == bid), None)
        if not b:
            return {"ok": False, "error": "unknown bundle"}
        if b["status"] in ("queued", "building"):
            _cancel.add(bid)
            if b["status"] == "queued":
                b["status"] = "cancelled"
                _save(cfg, items)
        return {"ok": True, "status": b["status"]}


def delete(cfg: HunterConfig, bid: str) -> dict:
    with _lock:
        items = _load(cfg)
        b = next((x for x in items if x["id"] == bid), None)
        if not b:
            return {"ok": False, "error": "unknown bundle"}
        if b["status"] == "building":
            _cancel.add(bid)
        items = [x for x in items if x["id"] != bid]
        _save(cfg, items)
    for suffix in (".zip", ".zip.part", ".files.json"):
        try:
            os.remove(_dir(cfg) / f"{bid}{suffix}")
        except OSError:
            pass
    return {"ok": True}


def mark_served(cfg: HunterConfig, bid: str) -> None:
    with _lock:
        items = _load(cfg)
        for b in items:
            if b["id"] == bid:
                b["served"] = b.get("served", 0) + 1
                b["last_served_ts"] = int(time.time())
        _save(cfg, items)


def sweep(cfg: HunterConfig) -> dict:
    """Expire old bundles and enforce the on-disk cap (oldest ready first)."""
    _max_b, keep_bytes, _ttl_s, _max_f = _limits(cfg)
    now = time.time()
    removed = 0
    with _lock:
        items = _load(cfg)
        keep = []
        for b in items:
            if b.get("status") == "ready" and now > b.get("expires_ts", 0):
                removed += 1
                continue
            if b.get("status") in ("failed", "cancelled") and now - b.get("created_ts", 0) > 3600:
                removed += 1
                continue
            keep.append(b)
        ready = sorted([b for b in keep if b.get("status") == "ready"], key=lambda b: b.get("created_ts", 0))
        used = sum(b.get("bytes_done", 0) for b in keep if b.get("status") in ("ready", "building"))
        while used > keep_bytes and ready:
            victim = ready.pop(0)
            keep = [b for b in keep if b["id"] != victim["id"]]
            used -= victim.get("bytes_done", 0)
            removed += 1
        gone = {b["id"] for b in items} - {b["id"] for b in keep}
        _save(cfg, keep)
    for bid in gone:
        for suffix in (".zip", ".zip.part", ".files.json"):
            try:
                os.remove(_dir(cfg) / f"{bid}{suffix}")
            except OSError:
                pass
    return {"removed": removed}


# --------------------------------------------------------------------------
# builder thread
# --------------------------------------------------------------------------

def _ensure_worker(cfg: HunterConfig) -> None:
    global _worker, _cfg
    _cfg = cfg
    if _worker and _worker.is_alive():
        return
    _worker = threading.Thread(target=_loop, name="dl-builder", daemon=True)
    _worker.start()


def _loop() -> None:
    while True:
        _wake.wait(timeout=30)
        _wake.clear()
        cfg = _cfg
        if cfg is None:
            continue
        try:
            sweep(cfg)
        except Exception as exc:               # noqa: BLE001
            log.warning("download sweep failed: %s", exc)
        while True:
            with _lock:
                items = _load(cfg)
                b = next((x for x in items if x["status"] == "queued"), None)
                if not b:
                    break
                b["status"] = "building"
                b["started_ts"] = int(time.time())
                _save(cfg, items)
            _build(cfg, b["id"])


def _update(cfg: HunterConfig, bid: str, **fields) -> None:
    with _lock:
        items = _load(cfg)
        for b in items:
            if b["id"] == bid:
                b.update(fields)
        _save(cfg, items)


def _build(cfg: HunterConfig, bid: str) -> None:
    _max_b, _keep_b, ttl_s, _max_f = _limits(cfg)
    try:
        with open(_dir(cfg) / f"{bid}.files.json", encoding="utf-8") as f:
            files = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _update(cfg, bid, status="failed", error=f"manifest: {exc}")
        return
    final = _dir(cfg) / f"{bid}.zip"
    part = _dir(cfg) / f"{bid}.zip.part"
    done_b = done_n = skipped_n = 0
    last = 0.0
    used_arcs: set[str] = set()
    try:
        with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
            for fe in files:
                if bid in _cancel:
                    raise _Cancelled()
                src = fe["src"]
                try:
                    st = os.stat(src)
                except OSError:
                    skipped_n += 1
                    continue
                dt = time.localtime(st.st_mtime)[:6]
                if dt[0] < 1980:
                    dt = (1980, 1, 1, 0, 0, 0)
                arc = _unique_arc(fe["arc"], used_arcs)
                info = zipfile.ZipInfo(arc, date_time=dt)
                info.compress_type = zipfile.ZIP_STORED
                info.file_size = st.st_size
                with open(src, "rb") as fin, z.open(info, "w", force_zip64=True) as out:
                    while True:
                        if bid in _cancel:
                            raise _Cancelled()
                        buf = fin.read(1 << 20)
                        if not buf:
                            break
                        out.write(buf)
                        done_b += len(buf)
                        if time.time() - last > 1.0:
                            _update(cfg, bid, bytes_done=done_b, files_done=done_n,
                                    files_skipped=skipped_n)
                            last = time.time()
                done_n += 1
        with _lock:
            items = _load(cfg)
            exists = any(b["id"] == bid for b in items)
            if not exists or bid in _cancel:
                raise _Cancelled()
            os.replace(part, final)
            now = int(time.time())
            for b in items:
                if b["id"] == bid:
                    b.update(status="ready", bytes_done=done_b, files_done=done_n,
                             files_skipped=skipped_n, finished_ts=now,
                             expires_ts=now + ttl_s, bytes_total=max(done_b, 1))
            _save(cfg, items)
        log.info("download bundle %s ready: %d files (%d skipped), %.1f MB",
                 bid, done_n, skipped_n, done_b / 1e6)
    except _Cancelled:
        _update(cfg, bid, status="cancelled", bytes_done=done_b, files_done=done_n,
                files_skipped=skipped_n)
    except Exception as exc:                   # noqa: BLE001
        log.error("download bundle %s failed: %s", bid, exc)
        _update(cfg, bid, status="failed", error=f"{type(exc).__name__}: {exc}",
                bytes_done=done_b, files_done=done_n, files_skipped=skipped_n)
    finally:
        _cancel.discard(bid)
        try:
            os.remove(part)
        except OSError:
            pass
        try:
            os.remove(_dir(cfg) / f"{bid}.files.json")
        except OSError:
            pass


class _Cancelled(Exception):
    pass


def track_file(cfg: HunterConfig, item_id: int) -> tuple[Path, str] | None:
    """(host path, download filename) for one track — the no-bundle path."""
    conn = libquery.connect(cfg)
    try:
        r = conn.execute("SELECT i.path, i.title, i.artist, i.track FROM items i WHERE i.id = ?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    hp = domain.container_to_host_path(cfg, r["path"])
    if not hp or not hp.is_file():
        return None
    name = f"{_safe(r['artist'] or '')} - {(r['track'] or 0):02d} {_safe(r['title'] or hp.stem)}{hp.suffix}".strip(" -")
    return hp, name

"""Library mutations — the write side of the library manager.

hunter itself still never touches media or the beets DB. Everything here
is either:

* a **plan**: a read-only walk of musiclibrary.db (libquery) that tells the
  UI exactly what a removal would touch, with a fingerprint the apply call
  must echo back (no "the list changed under me" surprises), or
* a **job** in the beets container running ``jobs/library_remove.py``
  (journaled quarantine moves + Library-API row removal + manifest), or
* an attached exec of ``jobs/blocklist_ctl.py`` for the blocklist.

The hunter-side bookkeeping that belongs with a removal — parking open
wants, unfollowing, invalidating the owned-oracle, writing the event feed —
happens here too, so every entry point (UI, CLI) gets identical semantics.
It follows the job's OUTCOME, not its start (a saga: reserve → confirm or
compensate — see start_removal), and the UI asks the executor whether a
removal can run at all BEFORE queuing one (removal_preflight).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from hunter import blocklist, config as hunter_config, domain, libquery
from hunter.config import HunterConfig
from hunter.dockerctl import DockerClient, DockerError

if TYPE_CHECKING:
    from hunter.jobs import Jobs

log = logging.getLogger("hunter.libops")

PRUNE_SUBDIR = "library_prune"
HUGE = 5000                       # mirrors the script's --allow-huge cap
TYPED_CONFIRM_TRACKS = 30         # UI: ask to type the name above this


def _parse_result_json(text: str) -> dict | None:
    """Extract the last ``RESULT {...}`` JSON payload from script/job output."""
    for line in reversed((text or "").splitlines()):
        if line.startswith("RESULT "):
            try:
                parsed = json.loads(line[7:])
                return parsed if isinstance(parsed, dict) else None
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def _fingerprint(item_ids: list[int], album_ids: list[int]) -> str:
    h = hashlib.sha256()
    h.update(",".join(map(str, sorted(item_ids))).encode())
    h.update(b"|")
    h.update(",".join(map(str, sorted(album_ids))).encode())
    return h.hexdigest()[:16]


def _invalidate_library() -> None:
    from hunter import library
    fn = getattr(library, "invalidate_library_cache", None)
    if callable(fn):
        fn()


def removal_plan(cfg: HunterConfig, artists: list[str], album_ids: list[int],
                 item_ids: list[int], *, sample: int = 40) -> dict:
    """What a removal would touch. Pure read (mode=ro). Sizes are estimated
    from bitrate x length — a stat() per file over remote or FUSE storage would
    make a 2000-track artist plan take seconds; the job reports real bytes."""
    artists = [a for a in dict.fromkeys(a.strip() for a in artists) if a][:50]
    album_ids = sorted({int(a) for a in album_ids if int(a) > 0})[:2000]
    item_ids = sorted({int(i) for i in item_ids if int(i) > 0})[:HUGE + 1]
    album_cols = (
        "id, albumartist, album, year, mb_releasegroupid AS rgid,"
        " artpath IS NOT NULL AND artpath != '' AND artpath != X'' AS has_art"
    )
    item_cols = "id, album_id, albumartist, artist, album, title, length, bitrate, format"
    conn = libquery.connect(cfg)
    counts_by_aid: dict[int, int] = {}
    try:
        items: dict[int, dict] = {}
        albums: dict[int, dict] = {}
        for chunk in domain.chunked(artists, 400):
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT {album_cols} FROM albums WHERE albumartist IN ({qm})", chunk
            ):
                albums[r["id"]] = dict(r)
            for r in conn.execute(
                f"SELECT {item_cols} FROM items WHERE albumartist IN ({qm})", chunk
            ):
                items[r["id"]] = dict(r)
        for chunk in domain.chunked(album_ids, 400):
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT {album_cols} FROM albums WHERE id IN ({qm})", chunk
            ):
                albums[r["id"]] = dict(r)
            for r in conn.execute(
                f"SELECT {item_cols} FROM items WHERE album_id IN ({qm})", chunk
            ):
                items[r["id"]] = dict(r)
        for chunk in domain.chunked(item_ids, 400):
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT {item_cols} FROM items WHERE id IN ({qm})", chunk
            ):
                items[r["id"]] = dict(r)

        touched = {r["album_id"] for r in items.values() if r["album_id"]}
        full = set(albums)
        partial: set[int] = set()
        need_counts = sorted(touched - full)
        for chunk in domain.chunked(need_counts, 400):
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT album_id, COUNT(*) AS n FROM items WHERE album_id IN ({qm}) GROUP BY album_id",
                chunk,
            ):
                counts_by_aid[r["album_id"]] = int(r["n"])
        mine_by_aid = Counter(r["album_id"] for r in items.values() if r["album_id"])
        for aid in need_counts:
            total = counts_by_aid.get(aid, 0)
            mine = mine_by_aid.get(aid, 0)
            if total > 0 and mine >= total:
                full.add(aid)
            else:
                partial.add(aid)
        missing_album_meta = sorted((full | partial) - set(albums))
        for chunk in domain.chunked(missing_album_meta, 400):
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT {album_cols} FROM albums WHERE id IN ({qm})", chunk
            ):
                albums[r["id"]] = dict(r)
    finally:
        conn.close()

    by_album: dict[int, dict] = {}
    est_bytes = 0.0
    lossless = 0
    for r in items.values():
        is_ll = (r["format"] or "").upper() in domain.LOSSLESS_FORMATS
        if is_ll:
            lossless += 1
        br = r["bitrate"] or (900_000 if is_ll else 256_000)
        b = (r["length"] or 0) * br / 8.0
        est_bytes += b
        aid = r["album_id"] or 0
        a = by_album.setdefault(aid, {
            "album_id": aid, "artist": r["albumartist"] or r["artist"] or "",
            "album": r["album"], "year": albums.get(aid, {}).get("year"),
            "rgid": albums.get(aid, {}).get("rgid"),
            "has_art": albums.get(aid, {}).get("has_art"),
            "tracks": 0, "bytes": 0.0, "full": aid in full,
            "total": counts_by_aid.get(aid) if aid in partial else None,
        })
        a["tracks"] += 1
        a["bytes"] += b
    for aid in full:
        if aid not in by_album and aid in albums:
            ar = albums[aid]
            by_album[aid] = {"album_id": aid, "artist": ar["albumartist"] or "",
                             "album": ar["album"], "year": ar["year"],
                             "rgid": ar["rgid"], "has_art": ar["has_art"],
                             "tracks": 0, "bytes": 0.0, "full": True, "total": 0}
    rows = sorted(by_album.values(), key=lambda a: (str(a["artist"] or "").casefold(),
                                                     a["year"] or 0, str(a["album"] or "").casefold()))
    for a in rows:
        a["bytes"] = int(a["bytes"])
    artist_names = sorted({str(a["artist"]) for a in rows if a.get("artist")} | set(artists),
                          key=str.casefold)
    all_rgids = sorted(
        {str(albums[aid]["rgid"]) for aid in full if albums.get(aid, {}).get("rgid")}
    )
    plan = {
        "targets": {"artists": artists, "album_ids": album_ids, "item_ids": item_ids},
        "artists": artist_names,
        "tracks": len(items), "album_rows": len(full), "partial_albums": len(partial),
        "lossless": lossless, "est_bytes": int(est_bytes),
        "albums": rows[:sample], "all_album_ids": sorted(full), "all_rgids": all_rgids,
        "albums_total": len(rows),
        "fingerprint": _fingerprint(list(items), sorted(full)),
        "too_many": len(items) > HUGE,
        "typed_confirm": bool(artists) or len(items) > TYPED_CONFIRM_TRACKS,
        "blocked": sorted(blocklist.blocked_set(artist_names, cfg), key=str.casefold),
        "empty": not items and not full,
    }
    return plan


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def _park_note(job_id: str) -> str:
    return f"parked by removal job {job_id}"


def plan_park_keys(plan: dict) -> list[str]:
    """Canonical want keys associated with the full albums in a removal plan."""
    all_album_ids = plan.get("all_album_ids")
    if all_album_ids is None:
        all_album_ids = [
            a["album_id"] for a in plan.get("albums", []) if a.get("full") and a.get("album_id")
        ]
    keys = [
        f"{k}:beets:{aid}"
        for aid in all_album_ids
        if aid
        for k in ("gapfill", "upgrade")
    ]
    keys += [f"recovery:a{aid}" for aid in all_album_ids if aid]
    all_rgids = plan.get("all_rgids")
    if all_rgids is None:
        all_rgids = [
            a["rgid"] for a in plan.get("albums", []) if a.get("full") and a.get("rgid")
        ]
    for rgid in all_rgids:
        if rgid:
            keys.append(f"request:rg:{rgid}")
            keys.append(f"release:rg:{rgid}")
    return keys


def start_removal(cfg: HunterConfig, jobs: Jobs, plan: dict, *, block: bool,
                  reason: str, requested_by: str, drop_wants: bool = True,
                  unfollow: bool = True, allow_huge: bool = False) -> dict:
    """Start the beets-side job and RESERVE the hunter-side effects.

    Side effects follow the job's outcome, not its start, so a fast refusal
    never leaves wants dropped or artists logged as removed:

      reserve     now        park open wants under a marker tied to this job
      confirm     on ok      unfollow, log "removed", invalidate caches
      compensate  on refuse  restore the parked wants, log "removal_failed"

    The reserve step exists so no pass can re-download the artist in the
    window between "job started" and "files moved". on_library_job (the
    kind="library" finalizer) does the confirm/compensate half."""
    t = plan["targets"]
    job_id = jobs.new_id("library")
    argv = [
        hunter_config.beets_python_bin(cfg),
        hunter_config.beets_job_script(cfg, "library_remove.py"),
    ]
    for a in t["artists"]:
        argv.append(f"--artist={a}")
    for aid in t["album_ids"]:
        argv += ["--album-id", str(aid)]
    for iid in t["item_ids"]:
        argv += ["--item-id", str(iid)]
    argv += ["--apply", f"--requested-by={requested_by or 'ui'}", "--job-id", job_id]
    if block:
        argv.append("--block")
    if reason:
        argv.append(f"--reason={reason[:200]}")
    if allow_huge:
        argv.append("--allow-huge")
    label = _label(plan)
    # artist-wide parking ONLY for explicit artist targets — an album
    # removal must not park the artist's other wants
    park_keys = plan_park_keys(plan)
    side_plan = {"drop_wants": bool(drop_wants), "unfollow": bool(unfollow),
                 "artists": t["artists"], "park_keys": park_keys,
                 "label": label, "tracks": plan["tracks"],
                 "album_rows": plan["album_rows"], "block": bool(block),
                 "reason": reason, "requested_by": requested_by or "ui"}

    from hunter.state import HunterState
    parked = 0
    if drop_wants:
        with HunterState(cfg.state_db) as st:
            parked = st.park_wants(t["artists"], park_keys, _park_note(job_id))

    try:
        res = jobs.start_beets(
            kind="library", title=f"Remove {label}", argv=argv, mode="apply",
            meta={"op": "remove", "artists": t["artists"], "album_ids": t["album_ids"][:50],
                  "item_ids": t["item_ids"][:50], "tracks": plan["tracks"],
                  "album_rows": plan["album_rows"], "block": block, "reason": reason,
                  "requested_by": requested_by, "fingerprint": plan["fingerprint"],
                  "side": side_plan},
            exclusive="library:remove", job_id=job_id)
    except BaseException:
        if drop_wants and parked:
            with HunterState(cfg.state_db) as st:
                st.restore_parked(_park_note(job_id))
        raise
    if not res["ok"]:
        if drop_wants and parked:
            with HunterState(cfg.state_db) as st:
                st.restore_parked(_park_note(job_id))
        return res

    with HunterState(cfg.state_db) as st:
        st.log_event("removal", label, "",
                     f"started by {requested_by or 'ui'} · {plan['tracks']} tracks"
                     f" · {plan['album_rows']} albums{' · block' if block else ''}"
                     f"{' · ' + reason if reason else ''} · job {job_id}")
    res["side"] = {"wants_parked": parked, "unfollowed": [],
                   "deferred": "unfollow and the event feed land when the job finishes"}
    return res


def _run_for_job(cfg: HunterConfig, job: dict, side: dict) -> dict | None:
    """When a job died without a RESULT line (killed / lost), look for the
    manifest it may have written: exact job_id first, then legacy fallback."""
    runs = removals(cfg, limit=30)
    for s in runs:
        if s.get("broken"):
            continue
        if s.get("job_id") == job["id"]:
            return s
    want = set(side.get("artists") or [])
    for s in runs:
        if s.get("broken") or s.get("job_id"):
            continue
        if (s.get("ts") or 0) < job["started_ts"] - 5:
            continue
        if not s.get("moved_count"):
            continue
        got = set(((s.get("targets") or {}).get("artists")) or [])
        if want and got == want:
            return s
        if not want and s.get("requested_by") == side.get("requested_by"):
            return s
    return None


def _finish_removal(cfg: HunterConfig, job: dict, meta: dict) -> None:
    side = meta.get("side") or {}
    result = job_result(job) or {}
    label = side.get("label") or job.get("title") or job["id"]
    who = side.get("requested_by") or "ui"
    # "touched" = the script got as far as moving files; anything after that
    # must be confirmed (wants stay parked) even if it ended with errors.
    touched = bool(result.get("run_dir")) or bool(result.get("moved"))
    if not result and job["status"] in ("killed", "lost", "failed"):
        touched = _run_for_job(cfg, job, side) is not None
    from hunter.state import HunterState
    with HunterState(cfg.state_db) as st:
        if (job["status"] == "ok" and result.get("ok") is not False) or touched:
            unfollowed: list[str] = []
            if side.get("unfollow") and side.get("artists"):
                low = {n.casefold() for n in side["artists"]}
                for row in st.followed_artists():
                    if row["name"].casefold() in low and st.unfollow_artist(row["mbid"]):
                        unfollowed.append(row["name"])
            errors = int(result.get("errors") or 0)
            detail = (f"{result.get('moved', '?')} tracks → quarantine"
                      f" · {result.get('albums_removed', '?')} albums"
                      f"{' · blocked' if result.get('blocked') else ''}"
                      f"{f' · {errors} errors' if errors else ''}"
                      f"{' · unfollowed' if unfollowed else ''}"
                      f" · by {who}{' · ' + side['reason'] if side.get('reason') else ''}")
            for name in side.get("artists") or [None]:
                st.log_event("removed", name or "", "" if name else label, detail)
            if job["status"] != "ok" or result.get("ok") is False:
                st.log_event("removal_incomplete", label, "",
                             f"job {job['id']} ended {job['status']} after moving files"
                             " — wants stay parked; check Library → Removed")
            log.info("removal %s confirmed: %s", job["id"], detail)
        else:
            restored = st.restore_parked(_park_note(job["id"])) if side.get("drop_wants") else 0
            why = result.get("error") or (
                f"job {job['status']}"
                + (f" (exit {job.get('exit_code')})" if job.get("exit_code") is not None else ""))
            st.log_event("removal_failed", label, "",
                         f"{why} · {restored} parked want(s) restored · by {who}")
            log.warning("removal %s failed: %s (%d wants restored)", job["id"], why, restored)
    _invalidate_library()
    blocklist.invalidate()


def _finish_undo(cfg: HunterConfig, job: dict, meta: dict) -> None:
    result = job_result(job) or {}
    run_id = meta.get("removal") or ""
    who = meta.get("requested_by") or "ui"
    s = removal(cfg, run_id) or {}
    names = s.get("artists") or [run_id]
    from hunter.state import HunterState
    st = HunterState(cfg.state_db)
    try:
        if job["status"] == "ok" and result.get("ok"):
            for name in names:
                st.log_event("restored", name, "",
                             f"undo of {run_id} · {result.get('files', '?')} files"
                             f" · {result.get('rows', '?')} rows"
                             f"{' · unblocked' if result.get('unblocked') else ''} · by {who}")
        else:
            st.log_event("undo_failed", names[0] if names else run_id, "",
                         f"undo of {run_id}: {result.get('error') or job['status']} · by {who}")
    finally:
        st.close()
    _invalidate_library()
    blocklist.invalidate()


def on_library_job(jobs: Jobs, job: dict) -> None:
    """Finalizer for kind="library": the confirm/compensate half of
    start_removal and start_undo. Wired into Jobs from Layer 5 composition roots."""
    meta = job.get("meta") or {}
    op = meta.get("op")
    if op == "remove":
        _finish_removal(jobs.cfg, job, meta)
    elif op == "undo":
        _finish_undo(jobs.cfg, job, meta)


# --------------------------------------------------------------------------
# preflight — ask the executor, don't guess
# --------------------------------------------------------------------------

_PREFLIGHT_LOCK = threading.Lock()
_PREFLIGHT: dict[tuple[str, str], tuple[float, dict]] = {}


def removal_preflight(cfg: HunterConfig, *, max_age: float = 30.0,
                      force: bool = False, need_bytes: int = 0) -> dict:
    """Can a removal run RIGHT NOW, according to the container that would run
    it? `library_remove.py --preflight` checks that both storage roots are
    live mounts of the union, the quarantine is writable and has room."""
    cache_key = (
        str(getattr(cfg, "beets_container", "beets")),
        hunter_config.beets_quarantine_root_str(cfg),
    )
    with _PREFLIGHT_LOCK:
        now_mono = time.monotonic()
        cached = _PREFLIGHT.get(cache_key)
        if not force and cached and (now_mono - cached[0]) < max_age:
            res = dict(cached[1])
        else:
            docker = DockerClient(cfg.docker_sock)
            if not docker.available:
                res = {"ok": False, "error": "docker socket not mounted — beets console offline"}
            else:
                try:
                    r = docker.exec_run(
                        cfg.beets_container,
                        [
                            hunter_config.beets_python_bin(cfg),
                            hunter_config.beets_job_script(cfg, "library_remove.py"),
                            "--preflight",
                        ],
                        user=hunter_config.beets_exec_user_str(cfg),
                        timeout=25,
                    )
                    parsed = _parse_result_json(r["output"])
                    res = parsed if parsed is not None else {
                        "ok": False,
                        "error": f"preflight gave no RESULT (rc={r['exit_code']}): {r['output'][-200:]}",
                    }
                except DockerError as exc:
                    res = {"ok": False, "error": f"preflight failed: {exc}"}
            done_mono = time.monotonic()
            res["checked_ts"] = int(time.time())
            if res.get("ok"):
                _PREFLIGHT[cache_key] = (done_mono, dict(res))
    if res.get("ok") and need_bytes > 0:
        free = (res.get("quarantine") or {}).get("free_bytes")
        need = need_bytes * 1.05 + 1_000_000_000
        if free is not None and free < need:
            return {**res, "ok": False,
                    "error": f"insufficient free space on quarantine ({free} < {int(need)} bytes needed)"}
    return res


def _label(plan: dict) -> str:
    t = plan["targets"]
    if t["artists"]:
        names = t["artists"]
        return names[0] if len(names) == 1 else f"{names[0]} +{len(names) - 1} artists"
    if plan["albums"]:
        a = plan["albums"][0]
        head = f"{a['artist']} — {a['album']}"
        if len(plan["albums"]) > 1 or plan["albums_total"] > 1:
            head += f" +{plan['albums_total'] - 1} albums"
        return head if plan["album_rows"] else f"{plan['tracks']} tracks ({head})"
    return f"{plan['tracks']} tracks"


# --------------------------------------------------------------------------
# removals (manifests) + undo
# --------------------------------------------------------------------------

def prune_root(cfg: HunterConfig) -> Path:
    return cfg.quarantine_dir / PRUNE_SUBDIR


_RUN_RE = re.compile(r"^\d{8}_\d{6}_(?:[0-9a-f]{4}_)?[a-z0-9-]{1,40}$")


def removals(cfg: HunterConfig, limit: int = 60) -> list[dict]:
    root = prune_root(cfg)
    out: list[dict] = []
    if not root.is_dir():
        return out
    try:
        runs = sorted((d for d in root.iterdir() if d.is_dir() and _RUN_RE.match(d.name)),
                      key=lambda d: d.name, reverse=True)
    except OSError:
        return out
    for d in runs[:limit]:
        m = _read_manifest(d / "manifest.json")
        if not m:
            out.append({"id": d.name, "broken": True})
            continue
        out.append(_summary(m))
    return out


def _read_manifest(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _summary(m: dict) -> dict:
    res = m.get("result") or {}
    plan = m.get("plan") or {}
    moved_count = res.get("moved")
    if moved_count is None:
        moved_count = sum(1 for e in m.get("items", []) if e.get("moved"))
    return {
        "id": m.get("id"), "job_id": m.get("job_id") or "",
        "ts": m.get("ts"), "requested_by": m.get("requested_by"),
        "reason": m.get("reason"), "artists": plan.get("artists", []),
        "tracks": res.get("moved", len(m.get("items", []))),
        "moved_count": moved_count,
        "albums": res.get("albums_removed", len(m.get("albums", []))),
        "bytes": res.get("bytes", plan.get("bytes")), "errors": len(m.get("errors", [])),
        "blocked": m.get("blocked") or [], "undone_ts": m.get("undone_ts"),
        "complete": bool(res), "targets": m.get("targets"),
    }


def removal(cfg: HunterConfig, run_id: str) -> dict | None:
    if not _RUN_RE.match(run_id or ""):
        return None
    m = _read_manifest(prune_root(cfg) / run_id / "manifest.json")
    if not m:
        return None
    s = _summary(m)
    s["items"] = [{"item_id": e.get("item_id"), "src": e.get("src"), "dst": e.get("dst"),
                   "moved": e.get("moved"), "error": e.get("error"),
                   "title": (e.get("row") or {}).get("title"),
                   "artist": (e.get("row") or {}).get("artist"),
                   "album": (e.get("row") or {}).get("album")}
                  for e in m.get("items", [])[:2000]]
    s["album_rows"] = [{"album_id": a.get("album_id"),
                        "artist": (a.get("row") or {}).get("albumartist"),
                        "album": (a.get("row") or {}).get("album"),
                        "year": (a.get("row") or {}).get("year"),
                        "art": bool(a.get("art_dst"))} for a in m.get("albums", [])]
    s["extras"] = len(m.get("extras", []))
    s["errors_detail"] = m.get("errors", [])[:50]
    q_root = hunter_config.beets_quarantine_root_str(cfg)
    s["manifest"] = f"{q_root}/{PRUNE_SUBDIR}/{run_id}/manifest.json"
    s["run_dir"] = f"{q_root}/{PRUNE_SUBDIR}/{run_id}"
    return s


def start_undo(cfg: HunterConfig, jobs: Jobs, run_id: str, requested_by: str) -> dict:
    s = removal(cfg, run_id)
    if not s:
        return {"ok": False, "error": "unknown removal"}
    if s.get("undone_ts"):
        return {"ok": False, "error": "already undone"}
    res = jobs.start_beets(
        kind="library", title=f"Undo removal · {', '.join(s['artists'][:2]) or run_id}",
        argv=[
            hunter_config.beets_python_bin(cfg),
            hunter_config.beets_job_script(cfg, "library_remove.py"),
            "--undo", s["manifest"], "--apply",
        ],
        mode="apply",
        meta={"op": "undo", "removal": run_id, "requested_by": requested_by},
        exclusive="library:remove")
    if res["ok"]:
        # "restored" is logged by the finalizer once the files are back.
        from hunter.state import HunterState
        with HunterState(cfg.state_db) as st:
            st.log_event("undo", ", ".join(s["artists"][:3]) or run_id, "",
                         f"undo of {run_id} started by {requested_by or 'ui'} · job {res['job']['id']}")
    return res


def job_result(job: dict) -> dict | None:
    """The `RESULT {...}` line a finished library job printed, if any."""
    if not job or job.get("status") == "running":
        return None
    return _parse_result_json(domain.tail_file(job["log"], lines=6))


# --------------------------------------------------------------------------
# blocklist
# --------------------------------------------------------------------------

def blocklist_view(q: str = "", cfg: HunterConfig | None = None) -> dict:
    names = blocklist.entries(cfg)
    if q:
        ql = q.casefold()
        names = [n for n in names if ql in n.casefold()]
    return {"entries": names, "count": blocklist.size(cfg), "available": blocklist.available(cfg),
            "path": str(blocklist.path(cfg))}


def blocklist_edit(cfg: HunterConfig, docker: DockerClient, *, add: list[str],
                   remove: list[str]) -> dict:
    """Attached exec of blocklist_ctl.py (fast: one file rewrite)."""
    out: dict = {"ok": True, "added": [], "skipped": [], "removed": [], "missing": []}
    if not docker.available:
        return {"ok": False, "error": "docker socket not mounted"}
    changed = False
    try:
        for op, names in (("add", add), ("remove", remove)):
            names = [n.strip() for n in names if n and n.strip()][:200]
            if not names:
                continue
            try:
                res = docker.exec_run(
                    cfg.beets_container,
                    [
                        hunter_config.beets_python_bin(cfg),
                        hunter_config.beets_job_script(cfg, "blocklist_ctl.py"),
                        "--json", op, "--", *names,
                    ],
                    user=hunter_config.beets_exec_user_str(cfg),
                    timeout=30,
                )
            except DockerError as exc:
                out["ok"] = False
                out["error"] = str(exc)
                return out
            try:
                payload = json.loads(res["output"].strip().splitlines()[-1])
            except (ValueError, IndexError):
                out["ok"] = False
                out["error"] = (
                    f"blocklist_ctl {op} rc={res['exit_code']}: {res['output'][:200]}"
                )
                return out
            if not payload.get("ok"):
                out["ok"] = False
                out["error"] = payload.get("error", "blocklist edit failed")
                return out
            changed = True
            for k in ("added", "skipped", "removed", "missing"):
                out[k] += payload.get(k, [])
            out["count"] = payload.get("count")
    finally:
        if changed:
            blocklist.invalidate()
    return out


def blocklist_delta_for_wants(cfg: HunterConfig, names: list[str]) -> int:
    """Park open wants for freshly-blocked artists using normalized keys."""
    norm_keys = {domain.blocklist_norm(n) for n in names if domain.blocklist_norm(n)}
    if not norm_keys:
        return 0
    from hunter.state import HunterState
    with HunterState(cfg.state_db) as st:
        return st.drop_wants_for_blocked(norm_keys)


__all__ = [
    "HUGE",
    "TYPED_CONFIRM_TRACKS",
    "blocklist_delta_for_wants",
    "blocklist_edit",
    "blocklist_view",
    "job_result",
    "on_library_job",
    "plan_park_keys",
    "removal",
    "removal_preflight",
    "removal_plan",
    "removals",
    "start_removal",
    "start_undo",
]

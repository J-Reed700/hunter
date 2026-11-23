"""beets operations — the pipeline view and the curated script catalogue.

Everything here is a bounded file read on a read-only mount, a `mode=ro`
query, or a docker exec into the beets container that runs a script which
already has its own safety story (dry-run default, recovery moves instead
of deletes, flock guards). hunter never imports beets, never touches media.

Configured host/container paths:
    cfg.beets_automation_dir   automation root (ro): logs/, state/, jobs/
    cfg.beets_db.parent        beets config dir: musiclibrary.db, import.log
    cfg.music_root             music library mount (ro)
    cfg.quarantine_dir         beets quarantine mount (ro, optional)
    cfg.docker_sock            /var/run/docker.sock (ro) — exec into beets
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from hunter import domain
from hunter.config import HunterConfig
from hunter.dockerctl import DockerClient, DockerError
from hunter.jobs import Jobs, tail_file

log = logging.getLogger("hunter.beetsops")

# --------------------------------------------------------------------------
# Curated automation scripts (relative to cfg.beets_automation_container_dir/jobs)
# --------------------------------------------------------------------------
# tier: safe (dry-run default, reversible) · recovery (moves to
# quarantine/recovery, never deletes) · heavy (long, IO-bound) · maint.


def _cfg_from_jobs(jobs: Any, cfg: HunterConfig | None = None) -> HunterConfig | None:
    if cfg is not None:
        return cfg
    jcfg = getattr(jobs, "cfg", None)
    if jcfg is not None and isinstance(
        getattr(jcfg, "beets_automation_dir", None), (str, os.PathLike)
    ):
        return jcfg
    return None


def _beets_python(cfg: Any = None) -> str:
    val = getattr(cfg, "beets_python", None) if cfg is not None else None
    return val.strip() if isinstance(val, str) and val.strip() else "python3"


def _container_auto_dir(cfg: Any = None) -> str:
    val = (
        getattr(cfg, "beets_automation_container_dir", None)
        if cfg is not None
        else None
    )
    if isinstance(val, str) and val.strip():
        return val.strip().rstrip("/") or "/config/automation"
    return "/config/automation"


def _container_quarantine_root(cfg: Any = None) -> str:
    val = getattr(cfg, "beets_quarantine_root", None) if cfg is not None else None
    if isinstance(val, str) and val.strip():
        return val.strip().rstrip("/") or "/quarantine"
    return "/quarantine"


def _exec_argv(script: str, cfg: Any = None) -> list[str]:
    """Prefix the configured interpreter so script file modes never matter."""
    path = f"{_container_auto_dir(cfg)}/jobs/{script.lstrip('/')}"
    if script.endswith(".py"):
        return [_beets_python(cfg), path]
    if script.endswith(".sh"):
        return ["sh", path]
    return [path]


SCRIPTS: dict[str, dict] = {
    "maintenance": {
        "title": "Nightly maintenance (now)",
        "blurb": "DB backup → duplicates report → corrupt check → missing report → orphan prune → move → write → rescans → prune logs. flock-guarded; hours long.",
        "script": "maintenance.sh", "apply_flag": None, "tier": "maint",
        "exclusive": "maintenance",
    },
    "daily_summary": {
        "title": "Push daily summary",
        "blurb": "Send the daily library stats notification now.",
        "script": "daily_summary.sh", "apply_flag": None, "tier": "safe",
    },
    "sweep_empty_dirs": {
        "title": "Sweep empty album dirs",
        "blurb": "Remove leaf dirs under the music library holding no audio (cover-only leftovers). Never touches a dir with a track.",
        "script": "sweep_empty_album_dirs.py", "apply_flag": "--apply", "tier": "safe",
    },
    "recover_quarantine": {
        "title": "Recover quarantine",
        "blurb": "Album-aware resolver over <date>/unverified: relocate into existing albums, import genuinely new ones, park verified duplicates. Never singletons.",
        "script": "recover_quarantine.py", "apply_flag": "--apply", "tier": "recovery",
        "accepts_date": True, "accepts_limit": True,
    },
    "quarantine_retry": {
        "title": "Retry autotag on quarantine",
        "blurb": "Move a bounded batch back into the live pipeline for another AcoustID/MusicBrainz pass (the weekly job, on demand).",
        "script": "quarantine_retry.sh", "apply_flag": None, "tier": "recovery",
        "accepts_date": True, "date_env": "RETRY_DATE",
    },
    "dedup_masters": {
        "title": "Collapse duplicate masters",
        "blurb": "Keep the best master per track (24-bit > 16-bit > bitrate), move the rest to quarantine recovery with a manifest. Skips ambiguous groups.",
        "script": "dedup_masters.py", "apply_flag": "--apply", "tier": "recovery",
        "accepts_limit": True,
    },
    "fix_missing_art": {
        "title": "Fill missing album art",
        "blurb": "fetchart + embedart for albums with no cover, time-boxed, through the HTTP-timeout wrapper (ImageMagick capped). Never overwrites existing art.",
        "script": "fix_missing_art.py", "apply_flag": "--apply", "tier": "safe",
        "accepts_limit": True,
    },
    "import_untracked": {
        "title": "Import untracked album dirs",
        "blurb": "beet import -A over album dirs on disk beets doesn't know about (skips /Singles/). duplicate_action: merge keeps it non-destructive.",
        "script": "batch_import_untracked.py", "apply_flag": "--apply", "tier": "safe",
        "accepts_limit": True,
    },
    "prune_orphan_rows": {
        "title": "Prune orphan DB rows",
        "blurb": "Rows whose file is gone. Journaled, mount + blast-radius circuit breakers; rows only, never files.",
        "script": "prune_orphan_rows.py", "apply_flag": "--apply", "tier": "safe",
    },
    "normalize_genres": {
        "title": "Normalize genres",
        "blurb": "Collapse casing/accent duplicates and 'X Music' suffixes into canonical genre spellings (in-process Library API; reversible).",
        "script": "normalize_genres.py", "apply_flag": "--apply", "tier": "safe",
        "accepts_limit": True,
    },
    "cleanup_lonetrack_comps": {
        "title": "Clean lone-track V/A comps",
        "blurb": "Move 1-track 'Various Artists' compilation rows to recovery on demand.",
        "script": "cleanup_lonetrack_comps.py", "apply_flag": "--apply", "tier": "recovery",
    },
    "consolidate_collab_artists": {
        "title": "Consolidate collab artists",
        "blurb": "Retag 'A & B' / 'A × B' ghost albumartists to the lead established artist. Never deletes.",
        "script": "consolidate_collab_artists.py", "apply_flag": "--apply", "tier": "safe",
        "accepts_limit": True,
    },
    "tag_from_audiomuse": {
        "title": "Tag BPM/mood from AudioMuse",
        "blurb": "Write BPM, mood and key from AudioMuse's analysis into file tags (provider-agnostic).",
        "script": "tag_from_audiomuse.py", "apply_flag": "--apply", "tier": "safe",
        "accepts_limit": True,
    },
    "sweep_corrupted": {
        "title": "Sweep corrupted files",
        "blurb": "Decode-check the library (workers capped) and quarantine files that fail. Dry-run reports only.",
        "script": "sweep_corrupted_files.py", "apply_flag": "--apply", "tier": "heavy",
        "accepts_limit": True,
    },
    "rg_backfill": {
        "title": "ReplayGain backfill",
        "blurb": "Compute ReplayGain for items still missing it (the nightly job, on demand).",
        "script": "rg_backfill.sh", "apply_flag": None, "tier": "heavy",
        "exclusive": "rg_backfill",
    },
    "resilient_import": {
        "title": "Import a download dir",
        "blurb": "The watcher's own cascade (autotag → resolver → quarantine) on one directory.",
        "script": "resilient_import.py", "apply_flag": None, "tier": "safe", "internal": True,
    },
    "smart_merge": {
        "title": "Smart-merge partial album",
        "blurb": "Fill an existing partial album from a download dir by MB track id; dupes → quarantine, never deleted.",
        "script": "smart_merge_import.py", "apply_flag": "--apply", "tier": "recovery", "internal": True,
    },
}


def script_catalogue(jobs: Jobs, cfg: HunterConfig | None = None) -> dict:
    eff_cfg = _cfg_from_jobs(jobs, cfg)
    jobs_dir: Path | None = None
    if eff_cfg is not None and getattr(eff_cfg, "beets_automation_dir", None) is not None:
        jobs_dir = Path(eff_cfg.beets_automation_dir) / "jobs"
    recent = jobs.list(limit=200, kind="script")
    latest: dict[str, dict] = {}
    for r in reversed(recent):
        latest[r["meta"].get("script", "")] = r
    out = []
    for sid, spec in SCRIPTS.items():
        if spec.get("internal"):
            continue
        if jobs_dir is not None and not (jobs_dir / spec["script"]).is_file():
            continue
        out.append({"id": sid, "title": spec["title"], "blurb": spec["blurb"],
                    "dry_run": bool(spec["apply_flag"]), "tier": spec["tier"],
                    "accepts_date": bool(spec.get("accepts_date")),
                    "accepts_limit": bool(spec.get("accepts_limit")),
                    "last": latest.get(sid)})
    return {"scripts": out}


def start_script(jobs: Jobs, script_id: str, *, apply: bool = False,
                 date: str | None = None, limit: int | None = None,
                 extra: list[str] | None = None,
                 cfg: HunterConfig | None = None) -> dict:
    spec = SCRIPTS.get(script_id)
    if not spec:
        return {"ok": False, "error": f"unknown script {script_id!r}"}
    if spec.get("internal"):
        return {"ok": False, "error": f"script {script_id!r} is internal"}
    eff_cfg = _cfg_from_jobs(jobs, cfg)
    argv = _exec_argv(spec["script"], eff_cfg)
    env: dict[str, str] = {}
    if date:
        if not spec.get("accepts_date"):
            return {"ok": False, "error": "this job does not take a date"}
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            return {"ok": False, "error": "date must be YYYY-MM-DD"}
        if spec.get("date_env"):
            env[spec["date_env"]] = date
        else:
            argv += ["--date", date]
    if limit is not None:
        if not spec.get("accepts_limit"):
            return {"ok": False, "error": "this job does not take a limit"}
        lim = int(limit)
        if lim <= 0 or lim > 100_000:
            return {"ok": False, "error": "limit must be between 1 and 100000"}
        argv += ["--limit", str(lim)]
    allowed_flags: frozenset[str] = spec.get("allowed_flags") or frozenset()
    for tok in extra or []:
        tok_s = str(tok)
        if tok_s not in allowed_flags:
            return {"ok": False, "error": f"argument {tok_s!r} is not allowed for {script_id}"}
        argv.append(tok_s)
    if apply and spec["apply_flag"]:
        argv.append(spec["apply_flag"])
    mode = "apply" if (apply or not spec["apply_flag"]) else "dry-run"
    return jobs.start_beets(kind="script", title=spec["title"], argv=argv,
                            mode=mode, env=env or None,
                            exclusive=spec.get("exclusive") or f"script:{script_id}",
                            meta={"script": script_id, "date": date,
                                  "limit": limit})


# --------------------------------------------------------------------------
# tiny TTL cache (slow/network storage walks can take seconds, not ms)
# --------------------------------------------------------------------------

class _Cache:
    def __init__(self):
        self._d: dict = {}
        self._key_locks: dict = {}
        self._lock = threading.Lock()

    def _lock_for(self, key) -> threading.Lock:
        with self._lock:
            lk = self._key_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._key_locks[key] = lk
            return lk

    def get(self, key, ttl, producer):
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        with self._lock_for(key):
            now = time.monotonic()
            with self._lock:
                hit = self._d.get(key)
                if hit and now - hit[0] < ttl:
                    return hit[1]
            value = producer()
            if isinstance(value, dict) and (
                value.get("ok") is False or value.get("exec_ok") is False
            ):
                return value
            done_at = time.monotonic()
            with self._lock:
                self._d[key] = (done_at, value)
            return value

    def drop(self, key):
        with self._lock:
            self._d.pop(key, None)
            prefix = f"{key}:"
            for k in [k for k in self._d if isinstance(k, str) and k.startswith(prefix)]:
                self._d.pop(k, None)


cache = _Cache()


# --------------------------------------------------------------------------
# import watcher
# --------------------------------------------------------------------------

_LINE_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+)\s+([\w.]+): (.*)$")
_EVENT_RULES = (
    ("imported", re.compile(r"imported successfully")),
    ("quarantined", re.compile(
        r"— quarantining|routed \d+ files? to /\S+|\b[1-9]\d* quarantined")),
    ("queued", re.compile(r"^queued \[")),
    ("importing", re.compile(r"resilient import: |running: beet import")),
    ("rescan", re.compile(r"\brescan")),
)
_TALLY_RE = re.compile(
    r"cascade: \d+ initial → (\d+) imported across (\[[^\]]*\]), (\d+) quarantined")
_ROUTED_RE = re.compile(r"routed (\d+) files? to /\S+")
_QUARANTINE_PHASES = frozenset({"blocklist", "not_wanted", "year_filter"})


def _classify(msg: str) -> str:
    for kind, pattern in _EVENT_RULES:
        if pattern.search(msg):
            return kind
    return "info"


def watcher_log(cfg: HunterConfig) -> Path:
    return cfg.beets_automation_dir / "logs" / "import_watcher.log"


def watcher_status(cfg: HunterConfig, docker: DockerClient, ttl: int = 15) -> dict:
    def produce():
        alive, detail, exec_ok = False, "", True
        try:
            res = docker.exec_run(cfg.beets_container,
                                  ["pgrep", "-f", "python.*import_watcher"],
                                  user=cfg.beets_exec_user, timeout=20)
            alive = res["exit_code"] == 0 and bool(res["output"].strip())
            detail = res["output"].strip().replace("\n", ",")
        except DockerError as exc:
            exec_ok = False
            detail = str(exc)
        events, counts = [], {"imported": 0, "quarantined": 0, "queued": 0}
        raw = tail_file(watcher_log(cfg), lines=500)
        last_ts = None
        for line in raw.splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            ts, level, logger, msg = m.groups()
            last_ts = ts
            kind = _classify(msg)
            tally = _TALLY_RE.search(msg)
            if tally:
                phases = {p.strip(" '\"") for p in tally.group(2).strip("[]").split(",") if p.strip(" '\"")}
                if not (phases and phases <= _QUARANTINE_PHASES):
                    counts["imported"] += int(tally.group(1))
                counts["quarantined"] += int(tally.group(3))
            elif (routed := _ROUTED_RE.search(msg)):
                counts["quarantined"] += int(routed.group(1))
            elif kind == "queued":
                counts["queued"] += 1
            if kind in ("imported", "quarantined", "importing", "queued") \
                    or level in ("WARNING", "ERROR"):
                events.append({"ts": ts, "level": level, "logger": logger,
                               "kind": kind, "msg": msg[:400]})
        log_age = None
        try:
            log_age = int(time.time() - os.path.getmtime(watcher_log(cfg)))
        except OSError:
            pass
        return {"alive": alive, "exec_ok": exec_ok, "pids": detail,
                "last_log_ts": last_ts, "log_age_s": log_age,
                "counts": counts, "events": list(reversed(events))[:60],
                "container": docker.state(cfg.beets_container)}
    return cache.get("watcher_status", ttl, produce)


def restart_watcher(cfg: HunterConfig, docker: DockerClient) -> dict:
    try:
        res = docker.exec_run(cfg.beets_container,
                              ["pkill", "-f", "python.*import_watcher"],
                              user=cfg.beets_exec_user, timeout=20)
    except DockerError as exc:
        return {"ok": False, "error": str(exc)}
    cache.drop("watcher_status")
    return {"ok": res["exit_code"] in (0, 1), "exit_code": res["exit_code"],
            "output": res["output"][-400:]}


# --------------------------------------------------------------------------
# watcher config + download sources
# --------------------------------------------------------------------------

_CONFIG_PREFIXES = ("BEETS_", "WATCHER_", "NOTIFY_", "NAVIDROME_", "PLEX_",
                    "JELLYFIN_")
_CONFIG_EXTRA = ("PUID", "PGID", "TZ")
_SECRET_HINTS = ("TOKEN", "PASS", "KEY", "WEBHOOK", "SECRET")
_AUDIO = domain.AUDIO_EXTENSIONS


def parse_sources(spec: str) -> list[dict]:
    out = []
    for triple in (spec or "").split(","):
        parts = triple.strip().split(":", 2)
        if len(parts) == 3 and parts[1] in ("album_per_dir", "flat"):
            out.append({"name": parts[0], "mode": parts[1], "path": parts[2]})
    return out


def watcher_config(cfg: HunterConfig, docker: DockerClient, ttl: int = 120) -> dict:
    def produce():
        try:
            env = docker.env_of(cfg.beets_container)
        except DockerError:
            env = {}
        rows = []
        for k in sorted(env):
            if not (k.startswith(_CONFIG_PREFIXES) or k in _CONFIG_EXTRA):
                continue
            v = env[k]
            secret = any(h in k for h in _SECRET_HINTS)
            rows.append({"key": k, "secret": secret,
                         "value": ("••••••" if v else "") if secret else v})
        return {"ok": bool(env), "rows": rows,
                "sources": parse_sources(env.get("BEETS_SOURCES", ""))}
    return cache.get("watcher_config", ttl, produce)


def _host_path(cfg: HunterConfig, container_path: str) -> Path | None:
    """Map a beets-container path to hunter's view, strictly confined."""
    return domain.container_to_host_path(cfg, container_path, allow_quarantine=True)


def source_dirs(cfg: HunterConfig, docker: DockerClient, ttl: int = 20,
                per_source: int = 60) -> dict:
    def produce():
        out = []
        for src in watcher_config(cfg, docker)["sources"]:
            entry = {**src, "ok": True, "dirs": [], "files": None, "total": 0}
            root = _host_path(cfg, src["path"])
            if root is None or not root.is_dir():
                entry["ok"] = False
                entry["error"] = "not mounted in hunter" if root is None else "missing"
                out.append(entry)
                continue
            try:
                if src["mode"] == "album_per_dir":
                    rows = []
                    with os.scandir(root) as it:
                        for e in it:
                            if not e.is_dir(follow_symlinks=False):
                                continue
                            st = e.stat()
                            rows.append({"name": e.name, "mtime": st.st_mtime})
                    rows.sort(key=lambda r: r["mtime"], reverse=True)
                    entry["total"] = len(rows)
                    entry["dirs"] = rows[:per_source]
                else:
                    n = 0
                    for _r, _d, files in os.walk(root):
                        n += sum(1 for f in files if os.path.splitext(f)[1].lower() in _AUDIO)
                        if n > 5000:
                            break
                    entry["files"] = n
            except OSError as exc:
                entry["ok"] = False
                entry["error"] = str(exc)[:160]
            out.append(entry)
        return {"sources": out}
    return cache.get(f"source_dirs:{int(per_source)}", ttl, produce)


def source_dir_files(cfg: HunterConfig, docker: DockerClient, source: str,
                     dirname: str) -> dict | None:
    res = resolve_source_dir(cfg, docker, source, dirname)
    if not res:
        return None
    src, c_path = res
    root = _host_path(cfg, c_path)
    files = []
    if root and root.is_dir():
        for r, _d, fs in os.walk(root):
            for f in sorted(fs):
                p = Path(r) / f
                if domain.resolve_under(root, p) is None:
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                files.append({"name": str(p.relative_to(root)), "size": size,
                              "audio": p.suffix.lower() in _AUDIO})
                if len(files) >= 400:
                    break
            if len(files) >= 400:
                break
    return {"source": src, "path": c_path, "files": files,
            "audio": sum(1 for f in files if f["audio"])}


def resolve_source_dir(cfg: HunterConfig, docker: DockerClient,
                       source_name: str, dirname: str):
    if not _safe_seg(dirname):
        return None
    for src in watcher_config(cfg, docker)["sources"]:
        if src["name"] == source_name and src["mode"] == "album_per_dir":
            c_path = f"{src['path'].rstrip('/')}/{dirname}"
            base = _host_path(cfg, src["path"])
            if base is not None and base.exists():
                if domain.resolve_under(base, base / dirname) is None:
                    return None
            elif _host_path(cfg, c_path) is None:
                return None
            return src, c_path
    return None


def import_dir(cfg: HunterConfig, docker: DockerClient, jobs: Jobs,
               source_name: str, dirname: str) -> dict:
    res = resolve_source_dir(cfg, docker, source_name, dirname)
    if not res:
        return {"ok": False, "error": "unknown source/dir"}
    src, c_path = res
    spec = SCRIPTS["resilient_import"]
    argv = _exec_argv(spec["script"], cfg) + [c_path, "--mode", src["mode"], "--name", src["name"]]
    return jobs.start_beets(kind="script", title=f"Import {dirname}", argv=argv,
                            mode="apply", exclusive=f"import:{c_path}",
                            meta={"script": "resilient_import", "dir": c_path})


def smart_merge(jobs: Jobs, c_path: str, apply: bool,
                cfg: HunterConfig | None = None) -> dict:
    spec = SCRIPTS["smart_merge"]
    argv = _exec_argv(spec["script"], _cfg_from_jobs(jobs, cfg)) + [c_path] + (["--apply"] if apply else [])
    return jobs.start_beets(kind="script", title=f"Smart-merge {Path(c_path).name}",
                            argv=argv, mode="apply" if apply else "dry-run",
                            exclusive=f"merge:{c_path}",
                            meta={"script": "smart_merge", "dir": c_path})


# --------------------------------------------------------------------------
# quarantine browser (ro)
# --------------------------------------------------------------------------

_Q_CAP = 500


def quarantine_root(cfg: HunterConfig) -> Path:
    return cfg.quarantine_dir / "music"


def quarantine_tree(cfg: HunterConfig, ttl: int = 120) -> dict:
    def produce():
        root = quarantine_root(cfg)
        if not root.is_dir():
            return {"ok": False, "error": f"{root} not mounted", "buckets": []}
        try:
            with os.scandir(root) as it:
                tops = [e for e in it if e.is_dir()]
        except OSError as exc:
            return {"ok": False, "error": str(exc), "buckets": []}
        tops.sort(key=lambda e: e.name, reverse=True)
        buckets, total = [], 0
        for top in tops[:40]:
            reasons = []
            try:
                with os.scandir(top.path) as it_top:
                    children = [c for c in it_top if c.is_dir()]
            except OSError:
                children = []
            for child in sorted(children, key=lambda c: c.name):
                n, capped = _count(child.path)
                reasons.append({"name": child.name, "count": n, "capped": capped})
                total += n
            own = 0
            if not children:
                own, _ = _count(top.path)
                total += own
            try:
                top_mtime = top.stat().st_mtime
            except OSError:
                continue
            buckets.append({"name": top.name, "reasons": reasons,
                            "total": sum(r["count"] for r in reasons) + own,
                            "mtime": top_mtime,
                            "is_date": bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", top.name))})
        by_reason: dict[str, int] = {}
        for b in buckets:
            for r in b["reasons"]:
                by_reason[r["name"]] = by_reason.get(r["name"], 0) + r["count"]
        return {"ok": True, "buckets": buckets, "total": total,
                "by_reason": by_reason, "path": str(root),
                "all_buckets": len(tops)}
    return cache.get("quarantine_tree", ttl, produce)


def _count(path: str) -> tuple[int, bool]:
    n = 0
    try:
        with os.scandir(path) as it:
            for _e in it:
                n += 1
                if n >= _Q_CAP:
                    return n, True
    except OSError:
        return 0, False
    return n, False


def _safe_seg(s: str) -> bool:
    return bool(s) and "/" not in s and s not in (".", "..") and "\x00" not in s


def quarantine_entries(cfg: HunterConfig, bucket: str, reason: str,
                       offset: int = 0, limit: int = 100, q: str = "") -> dict | None:
    if not (_safe_seg(bucket) and _safe_seg(reason)):
        return None
    qroot = quarantine_root(cfg)
    d = domain.resolve_under(qroot, qroot / bucket / reason)
    if d is None or not d.is_dir():
        return None
    rows = []
    try:
        with os.scandir(d) as it:
            for e in it:
                if q and q.lower() not in e.name.lower():
                    continue
                if domain.resolve_under(d, Path(e.path)) is None:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                rows.append({"name": e.name, "is_dir": e.is_dir(),
                             "mtime": st.st_mtime,
                             "size": 0 if e.is_dir() else st.st_size})
    except OSError:
        return None
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    total = len(rows)
    page = rows[offset:offset + limit]
    for r in page:
        if r["is_dir"]:
            p = d / r["name"]
            audio = 0
            reason_txt = ""
            try:
                with os.scandir(p) as it_p:
                    for f in it_p:
                        if f.is_file() and os.path.splitext(f.name)[1].lower() in _AUDIO:
                            audio += 1
                rt = p / "_reason.txt"
                if rt.is_file() and domain.resolve_under(p, rt) is not None:
                    reason_txt = rt.read_text(errors="replace")[:600]
            except OSError:
                pass
            r["audio"] = audio
            r["reason_txt"] = reason_txt
    q_root = _container_quarantine_root(cfg)
    return {"bucket": bucket, "reason": reason, "total": total, "entries": page,
            "container_path": f"{q_root}/music/{bucket}/{reason}"}


def quarantine_entry(cfg: HunterConfig, bucket: str, reason: str,
                     name: str) -> dict | None:
    if not (_safe_seg(bucket) and _safe_seg(reason) and _safe_seg(name)):
        return None
    qroot = quarantine_root(cfg)
    p = domain.resolve_under(qroot, qroot / bucket / reason / name)
    if p is None or not p.exists():
        return None
    files = []
    reason_txt = ""
    if p.is_dir():
        for r, _d, fs in os.walk(p):
            for f in sorted(fs):
                fp = Path(r) / f
                if domain.resolve_under(p, fp) is None:
                    continue
                try:
                    size = fp.stat().st_size
                except OSError:
                    size = 0
                files.append({"name": str(fp.relative_to(p)), "size": size,
                              "audio": fp.suffix.lower() in _AUDIO})
                if len(files) >= 400:
                    break
            if len(files) >= 400:
                break
        rt = p / "_reason.txt"
        if rt.is_file() and domain.resolve_under(p, rt) is not None:
            try:
                reason_txt = rt.read_text(errors="replace")[:4000]
            except OSError:
                pass
    else:
        try:
            sz = p.stat().st_size
        except OSError:
            sz = 0
        files.append({"name": p.name, "size": sz,
                      "audio": p.suffix.lower() in _AUDIO})
    q_root = _container_quarantine_root(cfg)
    return {"bucket": bucket, "reason": reason, "name": name, "is_dir": p.is_dir(),
            "files": files, "reason_txt": reason_txt,
            "container_path": f"{q_root}/music/{bucket}/{reason}/{name}"}


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------

def log_sources(cfg: HunterConfig) -> dict[str, tuple[str, str]]:
    a = cfg.beets_automation_dir
    return {
        "beets-watcher": ("file", str(a / "logs" / "import_watcher.log")),
        "beets-import": ("file", str(cfg.beets_db.parent / "import.log")),
        "beets-notifications": ("file", str(a / "logs" / "notifications.log")),
        "beets-maintenance": ("newest", str(a / "logs" / "maintenance_")),
        "beets-container": ("docker", cfg.beets_container),
        "hunter-container": ("docker", getattr(cfg, "hunter_container", "hunter") or "hunter"),
        "slskd-container": ("docker", getattr(cfg, "slskd_container", "slskd") or "slskd"),
        "navidrome-container": ("docker", getattr(cfg, "navidrome_container", "navidrome") or "navidrome"),
        "hunter-pass": ("newest", str(cfg.log_dir / "hunter_")),
    }


def _safe_mtime(p: str) -> float:
    try:
        return os.path.getmtime(p)
    except OSError:
        return -1.0


def newest_log(prefix: str) -> str | None:
    d, base = os.path.split(prefix)
    try:
        cands = [os.path.join(d, f) for f in os.listdir(d)
                 if f.startswith(base) and f.endswith(".log")]
    except OSError:
        return None
    if not cands:
        return None
    best = max(cands, key=_safe_mtime)
    return best if _safe_mtime(best) >= 0 else None


def read_log(cfg: HunterConfig, docker: DockerClient, name: str,
             lines: int) -> str | None:
    kp = log_sources(cfg).get(name)
    if not kp:
        return None
    kind, target = kp
    if kind == "file":
        return tail_file(target, lines=lines) or "<empty>"
    if kind == "newest":
        p = newest_log(target)
        return tail_file(p, lines=lines) if p else "<no log yet>"
    if kind == "docker":
        try:
            return docker.logs(target, lines=lines)
        except DockerError as exc:
            return f"<docker logs failed: {exc}>"
    return None


_REPORT_RE = re.compile(
    r"^(missing|bad|dupes|multimaster|stale_folders|foreign_final)_?[\w.-]*\.txt$")


def maintenance_reports(cfg: HunterConfig) -> list[dict]:
    d = cfg.beets_automation_dir / "logs"
    out = []
    try:
        for f in d.iterdir():
            if _REPORT_RE.match(f.name) and f.is_file():
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size,
                            "mtime": st.st_mtime})
    except OSError:
        pass
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out[:40]


def read_report(cfg: HunterConfig, name: str, lines: int = 400) -> str | None:
    if not _REPORT_RE.match(name or ""):
        return None
    p = cfg.beets_automation_dir / "logs" / name
    if not p.is_file():
        return None
    return tail_file(p, lines=lines)


# --------------------------------------------------------------------------
# maintenance + rescan
# --------------------------------------------------------------------------

def maintenance_status(cfg: HunterConfig, ttl: int = 20) -> dict:
    def produce():
        st = cfg.beets_automation_dir / "state"
        out: dict = {"ok": False}
        try:
            out = {"ok": True, **json.loads((st / "maintenance_last.json").read_text())}
        except (OSError, ValueError) as exc:
            out = {"ok": False, "error": str(exc)}
        try:
            out["lock_mtime"] = (st / "maintenance.lock").stat().st_mtime
        except OSError:
            out["lock_mtime"] = None
        p = newest_log(str(cfg.beets_automation_dir / "logs" / "maintenance_"))
        out["log"] = os.path.basename(p) if p else None
        try:
            out["log_mtime"] = os.path.getmtime(p) if p else None
        except OSError:
            out["log_mtime"] = None
        return out
    return cache.get("maintenance_status", ttl, produce)


def trigger_rescan(cfg: HunterConfig, docker: DockerClient,
                   app: str | None = None) -> dict:
    cmd = [_beets_python(cfg),
           f"{_container_auto_dir(cfg)}/services/trigger_rescan.py"]
    if app and app != "all":
        if app not in ("navidrome", "plex", "jellyfin", "emby"):
            return {"ok": False, "error": "app must be navidrome|plex|jellyfin|emby"}
        cmd += ["--app", app]
    try:
        res = docker.exec_run(cfg.beets_container, cmd,
                              user=getattr(cfg, "beets_exec_user", ""), timeout=120)
    except DockerError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": res["exit_code"] == 0, "exit_code": res["exit_code"],
            "output": res["output"][-2000:]}


def load_schedule(cfg: HunterConfig | None = None) -> list[dict]:
    """Load optional scheduled jobs from ``cfg.schedule_file`` or
    ``cfg.beets_automation_dir / "schedule.json"``. Returns ``[]`` if absent or invalid."""
    path: Path | None = None
    if cfg is not None:
        sf = getattr(cfg, "schedule_file", None)
        if sf is not None:
            path = Path(sf)
        elif getattr(cfg, "beets_automation_dir", None) is not None:
            path = Path(cfg.beets_automation_dir) / "schedule.json"
    if path is None:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]

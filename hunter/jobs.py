"""The job engine — every long-running thing the UI can start, tracked.

A *job* is one bounded process with a log file, a start/finish time, an exit
code and a status. Two runners:

  * ``beets``  — ``docker exec`` into the beets container as
    ``cfg.beets_exec_user`` (or the container's default user). The exec'd
    shell redirects stdout+stderr to ``<automation_dir>/logs/jobs/<id>.log``
    (visible to hunter under ``cfg.beets_automation_dir / "logs/jobs/<id>.log"``),
    records the child PID in a pidfile so the job can be cancelled, and
    appends ``exit=N`` when done — the only trustworthy exit code after a
    daemon restart.
  * ``local``  — a subprocess of hunter itself (``hunter pass``,
    ``hunter seed-gaps`` …) with its log under ``<state_dir>/logs/jobs/``.

Records live in the ``jobs`` table of hunter.db. Nothing here decides *what*
is safe to run — that is ``beetcmd`` (beet subcommand policy) and
``beetsops.SCRIPTS`` (the curated automation catalogue). This module only
runs argv lists it is handed, never shell strings from the network.

A job of a given kind may have a *finalizer* (register_finalizer): it runs
exactly once when the row leaves ``running``, in whichever process first
observes that — a UI poll, the scheduler's per-cycle refresh, the local
reaper thread. The claim is the UPDATE that flips the row: rowcount 1 means
"I settled it, I run the hook"; a concurrent reader sees 0 and does nothing.
That is how a removal's hunter-side bookkeeping follows the job's OUTCOME
rather than its start (libops.start_removal).

Progress is parsed heuristically from the log tail (``n/m``, ``NN%``,
``[n/m]``) — good enough for a progress bar, never load-bearing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from hunter import db as hunter_db
from hunter.config import HunterConfig
from hunter.dockerctl import DockerClient, DockerError

log = logging.getLogger("hunter.jobs")

# container-side paths (inside beets)
BEETS_JOB_LOG_DIR = "/config/automation/logs/jobs"
BEETS_JOB_PID_DIR = "/config/automation/state/jobs"

_KEEP_ROWS = 400
_LOG_KEEP_DAYS = 14
_MAX_CONCURRENT = {"beets": 3, "local": 2}

_PROGRESS_RES = (
    re.compile(r"\[(\d+)/(\d+)\]"),
    re.compile(r"\b(\d+)\s*/\s*(\d+)\b(?!\s*[km]?b)", re.IGNORECASE),
    re.compile(r"\b(\d{1,3}(?:\.\d)?)\s*%"),
)

# in-process handles for local jobs (reaped by a watcher thread each)
_local_procs: dict[str, subprocess.Popen] = {}
_lock = threading.Lock()
_START_LOCK = threading.Lock()

# kind → hook(jobs, settled_job_dict). Hooks must not raise; if one does it
# is logged and the job is still marked finalized (no retry storms).
_finalizers: dict[str, Callable[["Jobs", dict], None]] = {}


def register_finalizer(kind: str, fn: Callable[["Jobs", dict], None]) -> None:
    _finalizers[kind] = fn


def tracked_local_pids() -> set[int]:
    """Thread-safe snapshot of active local child PIDs owned by reaper threads."""
    with _lock:
        return {int(p.pid) for p in _local_procs.values() if getattr(p, "pid", None)}


def ensure_finalizers_registered() -> None:
    """Guarantee built-in finalizers are registered regardless of import order."""
    if "library" not in _finalizers:
        try:
            mod = sys.modules.get("hunter.libops")
            if mod is None:
                import hunter.libops as mod  # noqa: F401
            if "library" not in _finalizers and hasattr(mod, "_on_library_job"):
                _finalizers["library"] = mod._on_library_job
        except Exception:       # noqa: BLE001
            log.exception("failed to register built-in library finalizer")


def _conn(db: Path) -> sqlite3.Connection:
    return hunter_db.open_hunter_db(db, timeout=15.0)


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    try:
        d["argv"] = json.loads(d["argv"])
    except (TypeError, ValueError):
        d["argv"] = []
    try:
        d["meta"] = json.loads(d["meta"])
    except (TypeError, ValueError):
        d["meta"] = {}
    d["duration"] = ((d["finished_ts"] or int(time.time())) - d["started_ts"])
    d["command"] = shlex.join(d["argv"]) if d["argv"] else ""
    return d


def tail_file(path: str | Path, lines: int = 200,
              max_bytes: int = 512 * 1024) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-lines:])


def parse_progress(text: str) -> dict:
    """Best-effort {pct, done, total, last} from a log tail."""
    out: dict = {"pct": None, "done": None, "total": None, "last": ""}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return out
    out["last"] = lines[-1][-240:]
    for ln in reversed(lines[-40:]):
        if ln.startswith("exit=") or ln.startswith("#"):
            continue
        m = _PROGRESS_RES[0].search(ln) or _PROGRESS_RES[1].search(ln)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if 0 < total < 10_000_000 and done <= total:
                out.update(done=done, total=total,
                           pct=round(100.0 * done / total, 1))
                return out
        m = _PROGRESS_RES[2].search(ln)
        if m:
            pct = float(m.group(1))
            if 0 <= pct <= 100:
                out["pct"] = pct
                return out
    return out


class Jobs:
    def __init__(self, cfg: HunterConfig):
        self.cfg = cfg
        self.db = cfg.state_db
        self.docker = DockerClient(cfg.docker_sock)
        self.local_log_dir = cfg.log_dir / "jobs"
        # hunter-side view of the beets job log dir
        self.beets_log_dir = cfg.beets_automation_dir / "logs" / "jobs"

    def _beets_job_dirs(self) -> tuple[str, str]:
        raw = getattr(self.cfg, "beets_automation_container_dir", None)
        base = (
            raw.strip().rstrip("/")
            if isinstance(raw, str) and raw.strip()
            else "/config/automation"
        )
        if base == "/config/automation":
            return BEETS_JOB_LOG_DIR, BEETS_JOB_PID_DIR
        return f"{base}/logs/jobs", f"{base}/state/jobs"

    # ---- queries -------------------------------------------------------------

    def get(self, job_id: str, *, refresh: bool = True) -> dict | None:
        c = _conn(self.db)
        settled = False
        try:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            if refresh and row["status"] == "running":
                # Reconcile on read: the log footer / exec state is the truth.
                done, code, status = self._probe(row)
                if done:
                    settled = self._settle(c, row["id"], status, code)
                    row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            job = _row(row)
        finally:
            c.close()
        if settled:
            self._finalize(job)
        return job

    def _fetch(self, job_id: str) -> dict | None:
        """Plain read, no reconciliation."""
        c = _conn(self.db)
        try:
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        finally:
            c.close()

    def list(self, limit: int = 60, kind: str = "", status: str = "",
             offset: int = 0, *, refresh: bool = True) -> list[dict]:
        if refresh:
            self.refresh()
        where, args = [], []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if status == "running":
            where.append("status='running'")
        elif status == "finished":
            where.append("status NOT IN ('running', 'starting')")
        elif status:
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_ts DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        c = _conn(self.db)
        try:
            return [_row(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()

    def running(self, kind: str | None = None, *, refresh: bool = True) -> list[dict]:
        return [j for j in self.list(limit=50, status="running", refresh=refresh)
                if kind is None or j["kind"] == kind]

    def running_count(self, *, refresh: bool = True) -> int:
        return len(self.running(refresh=refresh))

    def find_running(self, predicate, *, refresh: bool = True) -> dict | None:
        for j in self.running(refresh=refresh):
            if predicate(j):
                return j
        return None

    def tail(self, job_id: str, lines: int = 200, *, refresh: bool = True) -> dict | None:
        job = self.get(job_id, refresh=refresh)
        if not job:
            return None
        text = tail_file(job["log"], lines=lines)
        return {"job": job, "text": text, "progress": parse_progress(text)}

    # ---- lifecycle -----------------------------------------------------------

    def refresh(self) -> None:
        """Reconcile every running row with reality; finished rows freeze.
        Rows THIS call settles get their finalizer run, once, here."""
        settled: list[str] = []
        now = int(time.time())
        c = _conn(self.db)
        try:
            c.execute(
                "UPDATE jobs SET status='failed', finished_ts=?"
                " WHERE status='starting' AND started_ts < ?",
                (now, now - 120),
            )
            c.commit()
            rows = c.execute(
                "SELECT * FROM jobs WHERE status='running'").fetchall()
            for r in rows:
                done, code, status = self._probe(r)
                if done and self._settle(c, r["id"], status, code):
                    settled.append(r["id"])
        finally:
            c.close()
        for job_id in settled:
            job = self._fetch(job_id)
            if job:
                self._finalize(job)

    @staticmethod
    def _settle(c: sqlite3.Connection, job_id: str, status: str,
                code: int | None) -> bool:
        """Flip running → terminal. True only for the ONE caller whose UPDATE
        landed; everyone else racing on the same row gets False."""
        cur = c.execute(
            "UPDATE jobs SET status=?, exit_code=?, finished_ts=?"
            " WHERE id=? AND status='running'",
            (status, code, int(time.time()), job_id))
        c.commit()
        return cur.rowcount == 1

    def _finalize(self, job: dict) -> None:
        ensure_finalizers_registered()
        fn = _finalizers.get(job["kind"])
        if fn is not None:
            try:
                fn(self, job)
            except Exception:                        # noqa: BLE001
                log.exception("finalizer for job %s (%s) failed", job["id"], job["kind"])
        meta = dict(job.get("meta") or {})
        meta["finalized_ts"] = int(time.time())
        c = _conn(self.db)
        try:
            c.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(meta), job["id"]))
            c.commit()
        finally:
            c.close()

    def _probe(self, r: sqlite3.Row) -> tuple[bool, int | None, str]:
        """(finished?, exit_code, status) for a running row."""
        footer = self._exit_from_log(r["log"])
        if r["runner"] == "local":
            proc = _local_procs.get(r["id"])
            if proc is not None:
                rc = proc.poll()
                if rc is None:
                    return False, None, "running"
                return True, rc, self._status_for(rc)
            # no handle: hunter restarted → trust the footer, else verify PID identity
            if footer is not None:
                return True, footer, self._status_for(footer)
            try:
                argv = json.loads(r["argv"])
            except (TypeError, ValueError):
                argv = []
            if r["pid"] and _pid_alive(r["pid"], argv):
                return False, None, "running"
            return True, None, "lost"
        # beets runner: inspect docker exec FIRST so stdout lines cannot fake completion
        info = self.docker.exec_inspect(r["exec_id"] or "")
        if info.get("running"):
            return False, None, "running"
        if info.get("unreachable"):
            # docker socket hiccup — only trust footer if already written, else keep running
            if footer is not None:
                return True, footer, self._status_for(footer)
            return False, None, "running"
        if footer is not None:
            return True, footer, self._status_for(footer)
        # exec is gone and no footer yet: brief flush grace or hard kill
        grace = time.time() - r["started_ts"] < 20
        if grace and info.get("exit_code") is None:
            return False, None, "running"
        if info.get("exit_code") is not None:
            rc = int(info["exit_code"])
            return True, rc, self._status_for(rc)
        return True, None, "lost"

    @staticmethod
    def _status_for(rc: int | None) -> str:
        if rc is None:
            return "lost"
        if rc == 0:
            return "ok"
        if rc in (-9, -15, 137, 143):
            return "killed"
        return "failed"

    @staticmethod
    def _exit_from_log(path: str) -> int | None:
        tail = tail_file(path, lines=3)
        for line in reversed(tail.splitlines()):
            if line.startswith("exit="):
                try:
                    return int(line[5:].strip())
                except ValueError:
                    return None
        return None

    def _insert(self, rec: dict) -> dict:
        c = _conn(self.db)
        try:
            c.execute(
                "INSERT INTO jobs (id, kind, title, argv, mode, runner, exec_id,"
                " pid, log, status, exit_code, started_ts, finished_ts, meta,"
                " origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["kind"], rec["title"], json.dumps(rec["argv"]),
                 rec["mode"], rec["runner"], rec.get("exec_id"), rec.get("pid"),
                 rec["log"], "running", None, rec["started_ts"], None,
                 json.dumps(rec.get("meta") or {}), rec.get("origin", "ui")))
            # bounded history
            c.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM jobs"
                      " WHERE status!='running' ORDER BY started_ts DESC"
                      " LIMIT -1 OFFSET ?)", (_KEEP_ROWS,))
            c.commit()
            return _row(c.execute("SELECT * FROM jobs WHERE id=?",
                                  (rec["id"],)).fetchone())
        finally:
            c.close()

    def _new_id(self, kind: str) -> str:
        return f"{time.strftime('%Y%m%d_%H%M%S')}_{kind}_{uuid.uuid4().hex[:6]}"

    def _guard(self, runner: str, kind: str, exclusive_key: str | None) -> str | None:
        running = self.running()
        if sum(1 for j in running if j["runner"] == runner) >= _MAX_CONCURRENT[runner]:
            return f"{_MAX_CONCURRENT[runner]} {runner} jobs already running — wait for one to finish"
        if exclusive_key:
            for j in running:
                if j["meta"].get("exclusive") == exclusive_key:
                    return f"already running: {j['title']} ({j['id']})"
        return None

    def _guard_and_reserve(self, rec: dict, runner: str, exclusive_key: str | None) -> str | None:
        """Atomically check active job limits + exclusive key under BEGIN IMMEDIATE
        and insert a 'starting' reservation row before releasing the DB lock."""
        now = int(time.time())
        c = _conn(self.db)
        try:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT id, title, runner, status, started_ts, meta FROM jobs"
                " WHERE status='running' OR (status='starting' AND started_ts >= ?)",
                (now - 120,),
            ).fetchall()
            if sum(1 for r in rows if r["runner"] == runner) >= _MAX_CONCURRENT[runner]:
                c.rollback()
                return f"{_MAX_CONCURRENT[runner]} {runner} jobs already running — wait for one to finish"
            if exclusive_key:
                for r in rows:
                    try:
                        m = json.loads(r["meta"] or "{}")
                    except (TypeError, ValueError):
                        m = {}
                    if m.get("exclusive") == exclusive_key:
                        c.rollback()
                        return f"already running: {r['title']} ({r['id']})"
            c.execute(
                "INSERT INTO jobs (id, kind, title, argv, mode, runner, exec_id,"
                " pid, log, status, exit_code, started_ts, finished_ts, meta,"
                " origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["kind"], rec["title"], json.dumps(rec["argv"]),
                 rec["mode"], rec["runner"], rec.get("exec_id"), rec.get("pid"),
                 rec["log"], "starting", None, rec["started_ts"], None,
                 json.dumps(rec.get("meta") or {}), rec.get("origin", "ui")),
            )
            c.commit()
            return None
        except Exception:
            try:
                c.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            c.close()

    def _activate_reservation(self, job_id: str, *, exec_id: str | None = None,
                              pid: int | None = None) -> dict:
        c = _conn(self.db)
        try:
            c.execute(
                "UPDATE jobs SET status='running', exec_id=COALESCE(?, exec_id),"
                " pid=COALESCE(?, pid) WHERE id=?",
                (exec_id, pid, job_id),
            )
            c.execute("DELETE FROM jobs WHERE id IN (SELECT id FROM jobs"
                      " WHERE status NOT IN ('running', 'starting') ORDER BY started_ts DESC"
                      " LIMIT -1 OFFSET ?)", (_KEEP_ROWS,))
            c.commit()
            return _row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        finally:
            c.close()

    def _release_reservation(self, job_id: str) -> None:
        c = _conn(self.db)
        try:
            c.execute("DELETE FROM jobs WHERE id=? AND status='starting'", (job_id,))
            c.commit()
        finally:
            c.close()

    # ---- beets runner ------------------------------------------------------

    def start_beets(self, *, kind: str, title: str, argv: list[str],
                    mode: str = "run", meta: dict | None = None,
                    env: dict[str, str] | None = None,
                    exclusive: str | None = None,
                    origin: str = "ui",
                    job_id: str | None = None) -> dict:
        """Detached exec in the beets container. `argv` is executed verbatim
        (no shell interpretation of its elements)."""
        if not argv:
            return {"ok": False, "error": "empty argv"}
        if not self.docker.available:
            return {"ok": False, "error": "docker socket not mounted — "
                    "beets jobs need /var/run/docker.sock"}
        meta = dict(meta or {})
        if exclusive:
            meta["exclusive"] = exclusive

        # Reconcile stale rows BEFORE acquiring _START_LOCK / BEGIN IMMEDIATE.
        self.refresh()
        jid = job_id or self._new_id(kind)
        rec = {"id": jid, "kind": kind, "title": title, "argv": argv,
               "mode": mode, "runner": "beets", "exec_id": None,
               "log": str(self.beets_log_dir / f"{jid}.log"),
               "started_ts": int(time.time()), "meta": meta, "origin": origin}
        with _START_LOCK:
            why = self._guard_and_reserve(rec, "beets", exclusive)
        if why:
            return {"ok": False, "error": why}

        c_log_dir, c_pid_dir = self._beets_job_dirs()
        c_log = f"{c_log_dir}/{jid}.log"
        c_pid = f"{c_pid_dir}/{jid}.pid"
        header = f"# hunter job {jid} [{kind} · {mode}] {title}"
        script = (
            f"mkdir -p {shlex.quote(c_log_dir)} {shlex.quote(c_pid_dir)}; "
            f"exec >>{shlex.quote(c_log)} 2>&1; "
            f"echo {shlex.quote(header)}; "
            f"echo \"# started $(date '+%F %T')\"; "
            f"echo {shlex.quote('# argv: ' + shlex.join(argv))}; "
            f"setsid {shlex.join(argv)} </dev/null & child=$!; "
            f"echo $child > {shlex.quote(c_pid)}; "
            f"wait $child; rc=$?; "
            f"echo \"# finished $(date '+%F %T')\"; echo \"exit=$rc\"; "
            f"rm -f {shlex.quote(c_pid)}"
        )
        try:
            res = self.docker.exec_run(
                self.cfg.beets_container, ["sh", "-c", script],
                user=getattr(self.cfg, "beets_exec_user", ""), detach=True, timeout=30,
                env=env or None)
            job = self._activate_reservation(jid, exec_id=res["exec_id"])
        except DockerError as exc:
            self._release_reservation(jid)
            return {"ok": False, "error": str(exc)}
        except BaseException:
            self._release_reservation(jid)
            raise
        log.info("job started [%s] %s: %s", jid, title, shlex.join(argv))
        return {"ok": True, "job": job}

    # ---- local runner ------------------------------------------------------

    def start_local(self, *, kind: str, title: str, argv: list[str],
                    mode: str = "run", meta: dict | None = None,
                    exclusive: str | None = None,
                    origin: str = "ui") -> dict:
        meta = dict(meta or {})
        if exclusive:
            meta["exclusive"] = exclusive

        self.refresh()
        job_id = self._new_id(kind)
        self.local_log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.local_log_dir / f"{job_id}.log"
        rec = {"id": job_id, "kind": kind, "title": title, "argv": argv,
               "mode": mode, "runner": "local", "pid": None,
               "log": str(log_path), "started_ts": int(time.time()),
               "meta": meta, "origin": origin}
        with _START_LOCK:
            why = self._guard_and_reserve(rec, "local", exclusive)
        if why:
            return {"ok": False, "error": why}

        fh = None
        proc = None
        try:
            fh = open(log_path, "ab")
            fh.write((f"# hunter job {job_id} [{kind} · {mode}] {title}\n"
                      f"# started {time.strftime('%F %T')}\n"
                      f"# argv: {shlex.join(argv)}\n").encode())
            fh.flush()
            with _lock:
                proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL,
                                        start_new_session=True)
                _local_procs[job_id] = proc
            job = self._activate_reservation(job_id, pid=proc.pid)
        except OSError as exc:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
            with _lock:
                _local_procs.pop(job_id, None)
            self._release_reservation(job_id)
            return {"ok": False, "error": f"spawn failed: {exc}"}
        except BaseException:
            with _lock:
                _local_procs.pop(job_id, None)
            self._release_reservation(job_id)
            raise

        def reap() -> None:
            rc = proc.wait()
            try:
                fh.write((f"# finished {time.strftime('%F %T')}\n"
                          f"exit={rc}\n").encode())
                fh.close()
            except OSError:
                pass
            with _lock:
                _local_procs.pop(job_id, None)
            self.refresh()
        threading.Thread(target=reap, daemon=True, name=f"job-{job_id}").start()
        log.info("job started [%s] %s: %s", job_id, title, shlex.join(argv))
        return {"ok": True, "job": job}

    # ---- cancel ------------------------------------------------------------

    def cancel(self, job_id: str, force: bool = False) -> dict:
        job = self.get(job_id)
        if not job:
            return {"ok": False, "error": "unknown job"}
        if job["status"] != "running":
            return {"ok": False, "error": f"job is {job['status']}"}
        sig = "KILL" if force else "TERM"
        sig_num = signal.SIGKILL if force else signal.SIGTERM
        if job["runner"] == "local":
            proc = _local_procs.get(job_id)
            pid = proc.pid if proc is not None else job.get("pid")
            if not pid:
                return {"ok": False, "error": "no pid recorded for local job"}
            try:
                os.killpg(int(pid), sig_num)
            except ProcessLookupError:
                return {"ok": False, "error": f"process group {pid} not found"}
            except PermissionError:
                try:
                    if proc is not None:
                        (proc.kill if force else proc.terminate)()
                    else:
                        os.kill(int(pid), sig_num)
                except OSError as exc:
                    return {"ok": False, "error": str(exc)}
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "signal": sig}

        c_log_dir, c_pid_dir = self._beets_job_dirs()
        pidfile = f"{c_pid_dir}/{job_id}.pid"
        c_log = f"{c_log_dir}/{job_id}.log"
        script = (
            f"p=$(cat {shlex.quote(pidfile)} 2>/dev/null); "
            f"if [ -n \"$p\" ]; then "
            f"  (kill -{sig} -- -$p 2>/dev/null || kill -{sig} $p 2>/dev/null) "
            f"  && {{ echo \"killed $p\"; "
            f"       printf '# cancel requested (SIG{sig}) %s\\n' \"$(date '+%F %T')\" >> {shlex.quote(c_log)} 2>/dev/null || true; }} "
            f"  || {{ echo \"kill-failed $p\"; exit 1; }}; "
            f"else echo no-pid; exit 1; fi"
        )
        try:
            res = self.docker.exec_run(self.cfg.beets_container,
                                       ["sh", "-c", script],
                                       user=getattr(self.cfg, "beets_exec_user", ""),
                                       timeout=20)
        except DockerError as exc:
            return {"ok": False, "error": str(exc)}
        out = res["output"].strip()[:200]
        if res.get("exit_code") != 0 or "killed " not in out:
            return {"ok": False, "error": out or f"cancel failed (rc={res.get('exit_code')})",
                    "signal": sig, "output": out}
        return {"ok": True, "signal": sig, "output": out}

    # ---- housekeeping ------------------------------------------------------

    def prune_logs(self) -> int:
        """Prune old writable local job logs (never touch ro beets logs)."""
        n = 0
        cutoff = time.time() - _LOG_KEEP_DAYS * 86_400
        try:
            for p in self.local_log_dir.iterdir():
                if p.suffix == ".log" and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
        except OSError:
            pass
        return n


def _pid_alive(pid: int, expected_argv: list[str] | None = None) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    proc_cmdline = Path(f"/proc/{int(pid)}/cmdline")
    try:
        raw = proc_cmdline.read_bytes()
    except OSError:
        return True
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p]
    if not parts:
        return False
    if expected_argv:
        head = os.path.basename(str(expected_argv[0]))
        joined = " ".join(parts)
        return head in joined or "hunter" in joined
    return True


def hunter_argv(*args: str) -> list[str]:
    """argv for running hunter's own CLI as a local job."""
    return [sys.executable, "-m", "hunter.cli", *args]

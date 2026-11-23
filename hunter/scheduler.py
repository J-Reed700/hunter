"""Container PID 1 — the pass supervisor.

Spawns `hunter pass` as a bounded subprocess, settles finished jobs via
`Jobs.refresh()` every ~30s during idle waits, recovers interrupted download
bundles on startup, reaps child processes so PID 1 never accumulates zombies,
and shuts down the HTTP UI server cleanly on SIGTERM/SIGINT.
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from hunter import __version__, domain
from hunter.config import HunterConfig
from hunter.notify import Notifier
from hunter.pipeline import FAILURE_ALERT_AFTER

log = logging.getLogger("hunter.scheduler")

KILL_GRACE = 120          # seconds past the pass deadline before SIGTERM -> SIGKILL
STOP_GRACE = 8.0          # seconds to wait after SIGTERM on shutdown before SIGKILL
TIMEOUT_GRACE = 10.0      # seconds to wait after SIGTERM on deadline timeout
JOB_REFRESH_EVERY = 30.0  # settle finished background jobs promptly

_stop_event = threading.Event()


def _handle_signal(_signum: int, _frame: Any) -> None:
    # Signal handlers must NEVER log (Python logging locks deadlock on re-entry).
    _stop_event.set()


def _reap_zombies(exclude_pids: set[int] | None = None) -> None:
    """Peek exited children via waitid(WNOWAIT) and reap only untracked orphans,
    never stealing exit codes from UI-launched local jobs or spawned passes."""
    skip = set(exclude_pids or ())
    try:
        from hunter import jobs as _jobs

        skip |= set(_jobs.tracked_local_pids())
    except Exception as exc:                        # noqa: BLE001
        log.debug("tracked_local_pids lookup failed: %s", exc)
    try:
        from hunter.web import common as _web_common

        skip |= set(_web_common.tracked_child_pids())
    except Exception as exc:                        # noqa: BLE001
        log.debug("tracked_child_pids lookup failed: %s", exc)

    while True:
        try:
            res = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except (ChildProcessError, OSError):
            break
        if res is None or not getattr(res, "si_pid", 0):
            break
        pid = int(res.si_pid)
        if pid in skip:
            break
        try:
            reaped_pid, _status = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            break
        if reaped_pid <= 0:
            break


def _write_supervisor(cfg: HunterConfig, payload: dict[str, Any]) -> None:
    path = cfg.state_dir / "supervisor.json"
    try:
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        domain.atomic_write_json(path, payload, indent=None)
    except OSError as exc:
        log.warning("supervisor.json write failed: %s", exc)


def _should_alert(streak: int, threshold: int) -> bool:
    return streak >= threshold and (streak == threshold or (streak - threshold) % 3 == 0)


def _terminate_proc(
    proc: subprocess.Popen[bytes], grace_s: float, *, force_rc: int | None = None
) -> int:
    """Send SIGTERM, wait up to *grace_s*, then escalate to SIGKILL."""
    try:
        proc.terminate()
    except OSError:
        pass
    grace_end = time.monotonic() + grace_s
    while time.monotonic() < grace_end:
        rc = proc.poll()
        if rc is not None:
            return rc
        time.sleep(0.2)
    try:
        proc.kill()
    except OSError:
        pass
    waited = proc.wait()
    return waited if force_rc is None else force_rc


def _wait_for_pass(proc: subprocess.Popen[bytes], budget_s: float) -> int:
    """Wait for *proc* in 0.5s slices, forwarding SIGTERM on shutdown or timeout."""
    deadline = time.monotonic() + budget_s
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc

        if _stop_event.is_set():
            log.info("shutdown requested — terminating active pass child")
            return _terminate_proc(proc, STOP_GRACE)

        if time.monotonic() >= deadline:
            log.error("pass exceeded %ss — sending SIGTERM", int(budget_s))
            rc = _terminate_proc(proc, TIMEOUT_GRACE, force_rc=-9)
            if rc == -9:
                log.error("pass ignored SIGTERM — SIGKILL")
            return rc

        _stop_event.wait(0.5)


def run_forever() -> int:
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _stop_event.clear()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    cfg = HunterConfig.from_env()
    notifier = Notifier(cfg.webhook_url, cfg.notify_min_interval, cfg.state_dir)

    try:
        from hunter import downloads

        downloads.recover_on_startup(cfg)
    except Exception as exc:                       # noqa: BLE001
        log.warning("download startup recovery failed: %s", exc)

    from hunter import libops
    from hunter.jobs import Jobs
    from hunter.web.server import start_ui

    http_server = start_ui(cfg)
    job_finalizers = {"library": libops.on_library_job}

    log.info(
        "hunter %s starting | interval=%ss apply=%s spotify=%s slskd=%s",
        __version__,
        cfg.interval,
        cfg.apply,
        "configured" if cfg.spotify_configured else "unconfigured",
        "configured" if cfg.slskd_configured else "unconfigured",
    )

    consecutive_failures = 0
    lock_busy_streak = 0
    last_job_refresh = 0.0

    try:
        while not _stop_event.is_set():
            cfg = HunterConfig.from_env()
            started_mono = time.monotonic()
            rc: int | None
            try:
                proc = subprocess.Popen([sys.executable, "-m", "hunter.cli", "pass"])
            except OSError as exc:
                log.error("failed to spawn pass: %s", exc)
                lock_busy_streak = 0
                consecutive_failures += 1
                rc = None
            else:
                budget = cfg.pass_deadline + KILL_GRACE
                rc = _wait_for_pass(proc, float(budget))
                if _stop_event.is_set() and rc not in (0, 3):
                    log.info("pass interrupted by shutdown (rc=%s)", rc)
                    _reap_zombies()
                    break
                if rc == 3:
                    lock_busy_streak += 1
                else:
                    lock_busy_streak = 0
                    consecutive_failures = 0 if rc == 0 else consecutive_failures + 1

            _reap_zombies()
            elapsed = max(0.0, time.monotonic() - started_mono)
            log.info(
                "cycle done rc=%s in %.0fs | failures=%d busy=%d | next in %ds",
                rc,
                elapsed,
                consecutive_failures,
                lock_busy_streak,
                max(0, int(cfg.interval - elapsed)),
            )
            _write_supervisor(
                cfg,
                {
                    "ts": int(time.time()),
                    "rc": rc,
                    "cycle_s": int(elapsed),
                    "consecutive_failures": consecutive_failures,
                    "lock_busy_streak": lock_busy_streak,
                },
            )
            try:
                Jobs(cfg, finalizers=job_finalizers).refresh()
                last_job_refresh = time.monotonic()
            except Exception as exc:               # noqa: BLE001
                log.warning("job refresh failed: %s", exc)

            if rc != 3 and _should_alert(consecutive_failures, FAILURE_ALERT_AFTER):
                notifier.send(
                    f"hunter: {consecutive_failures} consecutive pass failures "
                    "(rc != 0) — check `docker logs hunter`",
                    force=True,
                    bucket="alert",
                    tags=["hunter", "warning"],
                )
            if _should_alert(lock_busy_streak, FAILURE_ALERT_AFTER + 1):
                notifier.send(
                    f"hunter: pass lock held for {lock_busy_streak} consecutive "
                    "cycles — a pass is likely wedged; check `docker logs hunter`",
                    force=True,
                    bucket="alert",
                    tags=["hunter", "warning"],
                )

            wake_mono = started_mono + cfg.interval
            while not _stop_event.is_set() and time.monotonic() < wake_mono:
                _reap_zombies()
                if time.monotonic() - last_job_refresh >= JOB_REFRESH_EVERY:
                    try:
                        Jobs(cfg, finalizers=job_finalizers).refresh()
                    except Exception as exc:       # noqa: BLE001
                        log.warning("job refresh failed: %s", exc)
                    last_job_refresh = time.monotonic()
                rem = wake_mono - time.monotonic()
                if rem <= 0:
                    break
                _stop_event.wait(min(5.0, rem))
    finally:
        if http_server is not None:
            try:
                http_server.shutdown()
                http_server.server_close()
            except Exception as exc:               # noqa: BLE001
                log.debug("http server shutdown failed: %s", exc)

    log.info("scheduler stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(run_forever())

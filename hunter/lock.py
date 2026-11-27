"""Single-pass mutual exclusion via flock.

The scheduler is the only routine pass-runner, but a human can always
`docker exec hunter hunter pass` mid-cycle — this makes that collision
boring instead of corrupting. Kernel-owned locks (fcntl) release
automatically when the holder dies, so there is no stale-PID cleanup
to get wrong.
"""
from __future__ import annotations

import fcntl
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusy(Exception):
    pass


@contextmanager
def hold(lock_file: Path, *, blocking: bool = False) -> Iterator[None]:
    """Acquire an exclusive flock on *lock_file* without truncating it before
    the lock is held. Writes holder metadata after lock.

    When *blocking* is False (default), raises ``LockBusy`` immediately if held.
    When *blocking* is True, blocks until the lock is acquired.
    """
    lock_file = Path(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        handle = os.fdopen(fd, "r+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            raise LockBusy(str(lock_file)) from exc
        handle.seek(0)
        handle.truncate(0)
        cmd = " ".join(sys.argv[:3])
        handle.write(f"pid={os.getpid()} ts={int(time.time())} cmd={cmd}\n")
        handle.flush()
        yield
    finally:
        handle.close()   # closing releases the flock


def read_holder(lock_file: Path) -> str:
    """Return the recorded holder line from *lock_file*, or '' if absent."""
    try:
        return Path(lock_file).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _proc_locks_has_inode(st: os.stat_result) -> bool | None:
    """Read-only Linux /proc/locks probe: returns True/False if readable, else None.

    Avoids acquiring LOCK_EX during a probe so concurrent non-blocking ``hold()``
    callers never suffer spurious ``LockBusy`` races.
    """
    try:
        with open("/proc/locks", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    target = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    for line in lines:
        parts = line.split()
        # Format: <n>: FLOCK ADVISORY WRITE <pid> <maj>:<min>:<ino> 0 EOF
        if len(parts) >= 6 and parts[1] == "FLOCK" and parts[5].lower() == target:
            return True
    return False


def is_locked(lock_file: Path) -> bool:
    """Non-blocking probe: True when another file description holds LOCK_EX."""
    lock_file = Path(lock_file)
    try:
        st = lock_file.stat()
    except OSError:
        return False
    probed = _proc_locks_has_inode(st)
    if probed is not None:
        return probed
    try:
        fd = os.open(lock_file, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


__all__ = ["LockBusy", "hold", "is_locked", "read_holder"]

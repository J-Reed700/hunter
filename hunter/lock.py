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
def hold(lock_file: Path) -> Iterator[None]:
    """Acquire a non-blocking exclusive flock on *lock_file* without
    truncating it before the lock is held. Writes holder metadata after lock."""
    lock_file = Path(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        handle = os.fdopen(fd, "r+", encoding="utf-8")
    except Exception:
        os.close(fd)
        raise
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def is_locked(lock_file: Path) -> bool:
    """Non-blocking probe: True when another file description holds LOCK_EX."""
    lock_file = Path(lock_file)
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

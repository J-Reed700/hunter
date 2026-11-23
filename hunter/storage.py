"""Storage-bind truth for hunter's music library and optional quarantine roots.

summary() is what the healthcheck, the pass summary, /api/status and the
removal preflight read. When ``storage_fstype`` is ``None`` (the default),
any live filesystem is accepted; set ``HUNTER_STORAGE_FSTYPE`` to enforce
a specific filesystem type (e.g. ``fuse.mergerfs``).
"""
from __future__ import annotations

import errno as _errno
import os
from dataclasses import dataclass

MOUNTINFO = "/proc/self/mountinfo"
_UNSET = object()


def _unescape(s: str) -> str:
    return (
        s.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def enclosing_mount(path: str, mountinfo: str = MOUNTINFO) -> tuple[str, str] | None:
    """(mountpoint, fstype) of the deepest mount containing the already-resolved
    *path*; None when nothing in mountinfo covers it. Stacked mounts on one
    mountpoint share the path; the last line wins, which is the live one."""
    best: tuple[str, str] | None = None
    try:
        with open(mountinfo, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines:
        before, sep, after = line.partition(" - ")
        if not sep:
            continue
        fields = before.split()
        tail = after.split()
        if len(fields) < 5 or not tail:
            continue
        mp = _unescape(fields[4])
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) >= len(best[0]):
                best = (mp, tail[0])
    return best


@dataclass(frozen=True)
class MountProbe:
    path: str
    resolved: str
    mountpoint: str | None
    fstype: str | None
    exists: bool              # the leaf itself stats
    live: bool                # nearest existing ancestor stats (no ENOTCONN / EIO)
    errno: int | None
    error: str
    free_bytes: int | None

    def reason(
        self,
        required_fstype: str | None = None,
        *,
        must_exist: bool = True,
    ) -> str | None:
        """None when usable, else one operator-readable sentence."""
        if not self.live:
            return (
                f"{self.path} ({self.resolved}) is unreachable: {self.error} — "
                "stale storage mount; restart this container"
            )
        if self.mountpoint is None:
            return f"{self.resolved} is not on any mounted filesystem"
        if required_fstype and self.fstype != required_fstype:
            return (
                f"{self.resolved} is on {self.fstype} (mount {self.mountpoint}), "
                f"expected {required_fstype} — required filesystem is not mounted "
                "here"
            )
        if must_exist and not self.exists:
            return f"{self.resolved} does not exist"
        return None


def _fmt_errno(exc: OSError) -> tuple[int | None, str]:
    name = _errno.errorcode.get(exc.errno, exc.errno)
    return exc.errno, f"{name}: {exc.strerror}"


def probe(path: str | os.PathLike, *, mountinfo: str = MOUNTINFO) -> MountProbe:
    """Stat *path* — or, when the leaf is missing, its nearest existing
    ancestor — and say which mount it lands on and whether that mount answers."""
    asked = os.fspath(path)
    if isinstance(asked, bytes):
        asked = os.fsdecode(asked)
    try:
        resolved = os.path.realpath(asked)
    except OSError as exc:
        err_no, error = _fmt_errno(exc)
        mount = enclosing_mount(asked, mountinfo)
        return MountProbe(
            path=asked,
            resolved=asked,
            mountpoint=mount[0] if mount else None,
            fstype=mount[1] if mount else None,
            exists=False,
            live=False,
            errno=err_no,
            error=error,
            free_bytes=None,
        )

    mount = enclosing_mount(resolved, mountinfo)
    exists = live = False
    err_no: int | None = None
    error = ""
    free: int | None = None
    probe_at = resolved
    while True:
        try:
            os.stat(probe_at)
            live = True
            exists = probe_at == resolved
            break
        except FileNotFoundError:
            parent = os.path.dirname(probe_at)
            if parent == probe_at:
                error = "no existing ancestor"
                break
            probe_at = parent
        except OSError as exc:
            err_no, error = _fmt_errno(exc)
            break

    if live:
        # Only report statvfs free space when the probed ancestor sits on the
        # same mountpoint as *resolved* — an unmounted path falling back to
        # container / would otherwise report the root overlay's free space.
        probe_mount = enclosing_mount(probe_at, mountinfo)
        if mount is not None and probe_mount is not None and probe_mount[0] == mount[0]:
            try:
                st = os.statvfs(probe_at)
                free = st.f_bavail * st.f_frsize
            except OSError:
                free = None

    return MountProbe(
        path=asked,
        resolved=resolved,
        mountpoint=mount[0] if mount else None,
        fstype=mount[1] if mount else None,
        exists=exists,
        live=live,
        errno=err_no,
        error=error,
        free_bytes=free,
    )


def summary(
    cfg,
    *,
    required_fstype: str | None | object = _UNSET,
    mountinfo: str = MOUNTINFO,
) -> dict:
    """Storage health in one dict: {"ok", "merged": {...}, "quarantine": {...}}
    plus "error" joining all failing roots' reasons when not ok."""
    if required_fstype is _UNSET:
        if hasattr(cfg, "storage_fstype"):
            eff_fstype: str | None = cfg.storage_fstype
        else:
            eff_fstype = (
                os.environ.get("HUNTER_STORAGE_FSTYPE", "").strip() or None
            )
    else:
        eff_fstype = required_fstype  # type: ignore[assignment]

    out: dict = {"ok": True, "required_fstype": eff_fstype}
    errors: list[str] = []

    p_music = probe(cfg.music_root, mountinfo=mountinfo)
    why_music = p_music.reason(eff_fstype)
    out["merged"] = {
        "path": str(cfg.music_root),
        "resolved": p_music.resolved,
        "mountpoint": p_music.mountpoint,
        "fstype": p_music.fstype,
        "live": p_music.live,
        "exists": p_music.exists,
        "free_bytes": p_music.free_bytes,
        "error": why_music,
    }
    if why_music:
        errors.append(f"merged: {why_music}")

    if not getattr(cfg, "quarantine_enabled", True):
        out["quarantine"] = {
            "path": str(cfg.quarantine_dir),
            "configured": False,
            "live": True,
            "exists": False,
            "error": None,
        }
    else:
        p_q = probe(cfg.quarantine_dir, mountinfo=mountinfo)
        why_q = p_q.reason(eff_fstype)
        out["quarantine"] = {
            "path": str(cfg.quarantine_dir),
            "configured": True,
            "resolved": p_q.resolved,
            "mountpoint": p_q.mountpoint,
            "fstype": p_q.fstype,
            "live": p_q.live,
            "exists": p_q.exists,
            "free_bytes": p_q.free_bytes,
            "error": why_q,
        }
        if why_q:
            errors.append(f"quarantine: {why_q}")

    if errors:
        out["ok"] = False
        out["error"] = "; ".join(errors)
    return out


__all__ = ["MountProbe", "enclosing_mount", "probe", "summary"]

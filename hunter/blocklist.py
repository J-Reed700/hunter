"""Artist blocklist — external beets automation mirror OR native `/state` store.

When ``cfg.beets_automation_dir`` (or ``HUNTER_BEETS_AUTOMATION``) exists on
disk, Hunter reads ``<beets_automation_dir>/lib/artist_blocklist.txt`` so it
stays in lockstep with an external beets import watcher.

When no external automation directory is mounted (standard standalone Docker
deployments), Hunter automatically uses ``<state_dir>/artist_blocklist.txt``
with flock-guarded atomic writes — zero external scripts or Docker socket
required.

Uses ``domain.blocklist_norm()`` for artist identity and caches an immutable
``BlocklistSnapshot`` keyed on ``(path, st_mtime_ns, st_size, st_ino)`` without
ever poisoning the cache on transient ``OSError`` reads.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunter import domain, lock


@dataclass(frozen=True)
class BlocklistSnapshot:
    available: bool
    names: tuple[str, ...]
    keys: frozenset[str]


_EMPTY = BlocklistSnapshot(available=False, names=(), keys=frozenset())
_EMPTY_LOCAL = BlocklistSnapshot(available=True, names=(), keys=frozenset())

_lock = threading.Lock()
_cache_key: tuple[str, int, int, int] | None = None
_cache_snap: BlocklistSnapshot | None = None


def norm(s: str | None) -> str:
    return domain.blocklist_norm(s)


def uses_external(cfg: Any = None) -> bool:
    """True when an external beets automation directory is mounted on disk."""
    if cfg is not None and getattr(cfg, "beets_automation_dir", None) is not None:
        return Path(cfg.beets_automation_dir).is_dir()
    raw = (os.environ.get("HUNTER_BEETS_AUTOMATION") or "").strip()
    if raw:
        return Path(raw).is_dir()
    return Path("/beets-automation").is_dir()


def path(cfg: Any = None) -> Path:
    if uses_external(cfg):
        if cfg is not None and getattr(cfg, "beets_automation_dir", None) is not None:
            base = Path(cfg.beets_automation_dir)
        else:
            raw = (os.environ.get("HUNTER_BEETS_AUTOMATION") or "").strip()
            base = Path(raw or "/beets-automation")
        return base / "lib" / "artist_blocklist.txt"
    if cfg is not None and getattr(cfg, "state_dir", None) is not None:
        return Path(cfg.state_dir) / "artist_blocklist.txt"
    raw_state = (os.environ.get("HUNTER_STATE_DIR") or "").strip()
    if raw_state:
        return Path(raw_state) / "artist_blocklist.txt"
    raw_auto = (os.environ.get("HUNTER_BEETS_AUTOMATION") or "").strip()
    return Path(raw_auto or "/beets-automation") / "lib" / "artist_blocklist.txt"


def snapshot(cfg: Any = None) -> BlocklistSnapshot:
    global _cache_key, _cache_snap
    external = uses_external(cfg)
    p = path(cfg)
    p_str = str(p)
    with _lock:
        try:
            st = p.stat()
        except FileNotFoundError:
            if _cache_key is not None and _cache_key[0] == p_str:
                _cache_key = None
                _cache_snap = None
            return _EMPTY if external else _EMPTY_LOCAL
        except OSError:
            if _cache_key is not None and _cache_key[0] == p_str and _cache_snap is not None:
                return _cache_snap
            return _EMPTY

        stat_key = (p_str, st.st_mtime_ns, st.st_size, st.st_ino)
        if _cache_key == stat_key and _cache_snap is not None:
            return _cache_snap
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            if _cache_key is not None and _cache_key[0] == p_str and _cache_snap is not None:
                return _cache_snap
            return _EMPTY

        names: list[str] = []
        keys: set[str] = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.append(line)
            k = domain.blocklist_norm(line)
            if k:
                keys.add(k)
        snap = BlocklistSnapshot(
            available=True,
            names=tuple(names),
            keys=frozenset(keys),
        )
        _cache_key = stat_key
        _cache_snap = snap
        return snap


def entries(cfg: Any = None) -> list[str]:
    return list(snapshot(cfg).names)


def size(cfg: Any = None) -> int:
    return len(snapshot(cfg).keys)


def available(cfg: Any = None) -> bool:
    return snapshot(cfg).available


def is_blocked(name: str | None, cfg: Any = None) -> bool:
    if not name:
        return False
    k = domain.blocklist_norm(name)
    return bool(k) and k in snapshot(cfg).keys


def blocked_set(names: Iterable[str | None], cfg: Any = None) -> set[str]:
    """Subset of *names* that are blocked (exact strings echoed back)."""
    keys = snapshot(cfg).keys
    return {n for n in names if n and domain.blocklist_norm(n) in keys}


def _read_raw_lines(p: Path) -> list[str]:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def add_entries(names: Iterable[str], cfg: Any = None) -> tuple[list[str], list[str], int]:
    """Atomically append normalized-unique artist names to the native blocklist.

    Returns ``(added, skipped, total_count)``.
    """
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_file = p.parent / ".artist_blocklist.lock"
    with lock.hold(lock_file, blocking=True):
        lines = _read_raw_lines(p)
        existing_keys = {
            domain.blocklist_norm(ln.strip())
            for ln in lines
            if ln.strip() and not ln.strip().startswith("#")
        }
        existing_keys.discard("")
        added: list[str] = []
        skipped: list[str] = []
        for raw in names:
            name = (raw or "").strip().replace("\r", "").replace("\n", " ")
            if not name:
                continue
            k = domain.blocklist_norm(name)
            if not k:
                skipped.append(name)
                continue
            if k in existing_keys:
                skipped.append(name)
                continue
            existing_keys.add(k)
            lines.append(name)
            added.append(name)
        if added or not p.exists():
            content = ("\n".join(lines).rstrip("\n") + "\n") if lines else ""
            domain.atomic_write_text(p, content)
    invalidate()
    return added, skipped, len(existing_keys)


def remove_entries(names: Iterable[str], cfg: Any = None) -> tuple[list[str], list[str], int]:
    """Atomically remove matching normalized artist names from the native blocklist.

    Returns ``(removed, missing, total_count)``.
    """
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_file = p.parent / ".artist_blocklist.lock"
    with lock.hold(lock_file, blocking=True):
        lines = _read_raw_lines(p)
        existing_keys = {
            domain.blocklist_norm(ln.strip())
            for ln in lines
            if ln.strip() and not ln.strip().startswith("#")
        }
        existing_keys.discard("")
        drop_keys: set[str] = set()
        removed: list[str] = []
        missing: list[str] = []
        for raw in names:
            name = (raw or "").strip()
            if not name:
                continue
            k = domain.blocklist_norm(name)
            if k and k in existing_keys:
                drop_keys.add(k)
                removed.append(name)
                existing_keys.discard(k)
            else:
                missing.append(name)
        if drop_keys:
            kept: list[str] = []
            for ln in lines:
                s = ln.strip()
                if s and not s.startswith("#") and domain.blocklist_norm(s) in drop_keys:
                    continue
                kept.append(ln)
            content = ("\n".join(kept).rstrip("\n") + "\n") if kept else ""
            domain.atomic_write_text(p, content)
    invalidate()
    return removed, missing, len(existing_keys)


def invalidate() -> None:
    global _cache_key, _cache_snap
    with _lock:
        _cache_key = None
        _cache_snap = None


__all__ = [
    "BlocklistSnapshot",
    "add_entries",
    "available",
    "blocked_set",
    "entries",
    "invalidate",
    "is_blocked",
    "norm",
    "path",
    "remove_entries",
    "size",
    "snapshot",
    "uses_external",
]

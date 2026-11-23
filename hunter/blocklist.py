"""Read-side mirror of the beets artist blocklist.

Reads ``artist_blocklist.txt`` under ``cfg.beets_automation_dir / "lib"``
(or ``HUNTER_BEETS_AUTOMATION`` when *cfg* is omitted).
Uses ``domain.blocklist_norm()`` so both sides agree on artist identity,
and caches an immutable ``BlocklistSnapshot`` keyed on
``(path, st_mtime_ns, st_size, st_ino)`` without ever poisoning the cache on
transient ``OSError`` reads.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunter import domain


@dataclass(frozen=True)
class BlocklistSnapshot:
    available: bool
    names: tuple[str, ...]
    keys: frozenset[str]


_EMPTY = BlocklistSnapshot(available=False, names=(), keys=frozenset())

_lock = threading.Lock()
_cache_key: tuple[str, int, int, int] | None = None
_cache_snap: BlocklistSnapshot | None = None


def norm(s: str | None) -> str:
    return domain.blocklist_norm(s)


def path(cfg: Any = None) -> Path:
    if cfg is not None and getattr(cfg, "beets_automation_dir", None) is not None:
        base = Path(cfg.beets_automation_dir)
    else:
        raw = (os.environ.get("HUNTER_BEETS_AUTOMATION") or "").strip()
        base = Path(raw or "/beets-automation")
    return base / "lib" / "artist_blocklist.txt"


def snapshot(cfg: Any = None) -> BlocklistSnapshot:
    global _cache_key, _cache_snap
    p = path(cfg)
    p_str = str(p)
    with _lock:
        try:
            st = p.stat()
        except FileNotFoundError:
            if _cache_key is not None and _cache_key[0] == p_str:
                _cache_key = None
                _cache_snap = None
            return _EMPTY
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


def invalidate() -> None:
    global _cache_key, _cache_snap
    with _lock:
        _cache_key = None
        _cache_snap = None


__all__ = [
    "BlocklistSnapshot",
    "available",
    "blocked_set",
    "entries",
    "invalidate",
    "is_blocked",
    "norm",
    "path",
    "size",
    "snapshot",
]

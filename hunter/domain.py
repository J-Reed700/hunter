"""Pure domain foundations: constants, normalization, token matching,
path confinement, SQL helpers, and atomic filesystem writes.

Layer 0 — imports stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

LOSSLESS_FORMATS: frozenset[str] = frozenset({
    "FLAC", "ALAC", "WAV", "AIFF", "APE", "WV", "DSF",
})

LOSSLESS_SQL: str = "(" + ",".join(f"'{fmt}'" for fmt in sorted(LOSSLESS_FORMATS)) + ")"

AUDIO_EXTENSIONS: frozenset[str] = frozenset({
    ".flac", ".mp3", ".m4a", ".m4p", ".aac", ".ogg", ".opus",
    ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv", ".dsf",
    ".mpc", ".wma",
})

LOSSLESS_EXTENSIONS: frozenset[str] = frozenset({
    ".flac", ".ape", ".alac", ".wav", ".wv", ".aiff", ".aif", ".dsf",
})

# Priority semantics (ASC — lower integer hunts sooner).
PRIORITY_URGENT = -100
PRIORITY_OPERATOR = -50
PRIORITY_DEFAULT = 0
PRIORITY_MIN = -1000
PRIORITY_MAX = 100

_FEAT_RE = re.compile(
    r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring)\b.*$|"
    r"\s+(?:feat\.?|ft\.?|featuring)(?=\s|\.|$).*$",
    re.IGNORECASE,
)
_PARENS_EDITION_RE = re.compile(
    r"\s*[(\[](?![^)\]]*\blive\b)[^)\]]*\b(?:"
    r"deluxe|remaster(?:ed)?|expanded|anniversary|bonus|"
    r"edition|explicit|special|extended|version|reissue|mono|stereo"
    r")\b[^)\]]*[)\]]",
    re.IGNORECASE,
)
_DASH_EDITION_RE = re.compile(
    r"\s+[-–—]\s+(?!.*\blive\b).*\b(?:"
    r"deluxe|remaster(?:ed)?|expanded|anniversary|bonus|"
    r"edition|explicit|special|extended|version|reissue|mono|stereo"
    r")\b.*$",
    re.IGNORECASE,
)
_PUNCT_UNDERSCORE_RE = re.compile(r"[\W_]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")

_NUMWORDS: dict[str, str] = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
}
_NUMWORD_RE = re.compile(
    r"\b(?:" + "|".join(_NUMWORDS) + r")\b"
)
_THE_WORD_RE = re.compile(r"\bthe\b")


def _fold(text: str) -> str:
    """NFKD-decompose, strip combining marks, and Unicode casefold."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def normalize(text: str | None) -> str:
    """NFKD, strip diacritics, casefold, replace punctuation/underscores
    with spaces, and collapse whitespace."""
    if not text:
        return ""
    folded = _fold(text)
    cleaned = _PUNCT_UNDERSCORE_RE.sub(" ", folded)
    return _WS_RE.sub(" ", cleaned).strip()


def norm_artist(artist: str | None) -> str:
    """Normalize an artist name: strip feat. tail, drop leading 'the '
    only when non-empty remainder survives (keeps 'The The' -> 'the')."""
    if not artist:
        return ""
    base = _FEAT_RE.sub("", artist)
    out = normalize(base)
    if out.startswith("the "):
        rest = out[4:].strip()
        if rest:
            return rest
    return out


def norm_album(album: str | None) -> str:
    """Normalize an album title: strip non-live edition tags (parenthetical/
    bracketed and Spotify dash-delimited suffixes), preserving '(Live)' and
    '(Live Version)' so live records never collide with studio albums."""
    if not album:
        return ""
    stripped = _PARENS_EDITION_RE.sub("", album)
    stripped = _DASH_EDITION_RE.sub("", stripped)
    return normalize(stripped)


def blocklist_norm(text: str | None) -> str:
    """Canonical blocklist key matching beets' normalization rules:
    strip feat. tail, NFKD+casefold, map English number words 0..12,
    drop standalone 'the' only when non-empty remains ('The The' -> 'thethe'),
    and strip all non-alphanumeric characters including underscores."""
    if not text:
        return ""
    s = _FEAT_RE.sub("", text)
    s = _fold(s)
    s = _NUMWORD_RE.sub(lambda m: _NUMWORDS[m.group(0)], s)
    candidate = _THE_WORD_RE.sub("", s)
    if _PUNCT_UNDERSCORE_RE.sub("", candidate):
        s = candidate
    return _PUNCT_UNDERSCORE_RE.sub("", s)


def extract_match_tokens(norm_text: str | None) -> list[str]:
    """Extract search/match tokens from normalized text.

    Prefers tokens with ``len > 2``, falling back to ALL non-empty tokens
    when none exceed length 2 (handles U2, R.E.M., The XX, OK Go, 21, IV, X).
    """
    if not norm_text:
        return []
    tokens = [t for t in norm_text.split() if t]
    long_tokens = [t for t in tokens if len(t) > 2]
    return long_tokens if long_tokens else tokens


def tokens_match_path(
    tokens: list[str],
    hay_norm: str | None,
    require_all: bool = False,
) -> bool:
    """Match tokens against normalized haystack using whole-token set membership
    plus joined-initialism check (e.g. ``"rem"`` for ``["r", "e", "m"]``).

    Fails closed (returns False) when ``tokens`` or ``hay_norm`` is empty.
    """
    if not tokens or not hay_norm:
        return False
    hay_words = set(hay_norm.split())
    if not hay_words:
        return False
    matched = (
        all(t in hay_words for t in tokens)
        if require_all
        else any(t in hay_words for t in tokens)
    )
    if matched:
        return True
    if len(tokens) >= 2 and all(len(t) == 1 for t in tokens):
        return "".join(tokens) in hay_words
    return False


def resolve_under(base: Path, candidate: Path) -> Path | None:
    """Resolve symlinks on *candidate* and verify strict containment under
    *base.resolve()*. Rejects NUL bytes and returns None on any violation."""
    try:
        b_raw = os.fsdecode(os.fspath(base))
        c_raw = os.fsdecode(os.fspath(candidate))
        if "\x00" in b_raw or "\x00" in c_raw:
            return None
        resolved_base = Path(b_raw).resolve()
        resolved_cand = Path(c_raw).resolve()
        if not resolved_cand.is_relative_to(resolved_base):
            return None
        return resolved_cand
    except (OSError, ValueError):
        return None


def _match_prefix(s: str, prefix: str) -> str | None:
    p = prefix.rstrip("/")
    if not p:
        return s.lstrip("/") if s.startswith("/") else None
    if s == p:
        return ""
    if s.startswith(p + "/"):
        return s[len(p) + 1:].lstrip("/")
    return None


def _resolve_mapped_root(
    raw: str,
    base: Path,
    configured_root: Any,
    fallback_prefix: str,
) -> Path | None:
    cfg_s = configured_root.strip() if isinstance(configured_root, str) else ""
    prefixes = (cfg_s, str(base)) if cfg_s else (str(base), fallback_prefix)
    for prefix in dict.fromkeys(p for p in prefixes if p):
        rel = _match_prefix(raw, prefix)
        if rel is not None:
            cand = base / rel if rel else base
            return resolve_under(base, cand)
    return None


def container_to_host_path(
    cfg: Any,
    raw_path: str | bytes | None,
    allow_quarantine: bool = False,
) -> Path | None:
    """Map a beets-container path (e.g. under ``cfg.beets_music_root``, or
    ``cfg.beets_quarantine_root`` when *allow_quarantine* is True) onto
    hunter's host mount, strictly confined beneath ``cfg.music_root`` /
    ``cfg.quarantine_dir``.

    Returns None for unmapped paths, NUL bytes, or path traversals.
    """
    if raw_path is None:
        return None
    s = os.fsdecode(raw_path) if isinstance(raw_path, (bytes, bytearray)) else str(raw_path)
    if not s or "\x00" in s:
        return None

    hit = _resolve_mapped_root(
        s, Path(cfg.music_root), getattr(cfg, "beets_music_root", ""), "/merged"
    )
    if hit is not None:
        return hit

    if allow_quarantine:
        return _resolve_mapped_root(
            s,
            Path(cfg.quarantine_dir),
            getattr(cfg, "beets_quarantine_root", ""),
            "/quarantine",
        )

    return None


def dir_leaf(path: str | None) -> str:
    """Return the final non-empty path component across POSIX or Windows slashes."""
    if not path:
        return ""
    cleaned = str(path).rstrip("/\\").replace("\\", "/")
    return cleaned.rsplit("/", 1)[-1] if cleaned else ""


def extract_year(date_str: str | None) -> int:
    """Extract a leading 4-digit year from an ISO-like date string, or 0."""
    if not date_str:
        return 0
    s = str(date_str).strip()
    return int(s[:4]) if len(s) >= 4 and s[:4].isdigit() else 0


def extract_rgid_from_key(key: str | None) -> str | None:
    """Extract a MusicBrainz release-group ID from a want key, if present."""
    if not key:
        return None
    s = str(key)
    for prefix in ("request:rg:", "release:rg:"):
        if s.startswith(prefix):
            val = s[len(prefix):].strip()
            return val or None
    return None


def tail_file(path: str | os.PathLike[str], lines: int = 200, max_bytes: int = 262_144) -> str:
    """Read the last *lines* lines of *path* (bounded by *max_bytes*)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(raw.splitlines()[-lines:])


def escape_like(term: str, escape: str = "\\") -> str:
    """Escape LIKE metacharacters (escape char, %, _) for ``LIKE ? ESCAPE '\\'``."""
    return (
        term.replace(escape, escape + escape)
        .replace("%", escape + "%")
        .replace("_", escape + "_")
    )


def chunked(seq: Iterable[T], size: int = 400) -> Iterator[list[T]]:
    """Yield slices of *seq* of at most *size* items (safe below SQLite's limit)."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    buf: list[T] = []
    for item in seq:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    """Atomically write *data* to *path* via a sibling temp file + fsync + replace.

    When *mode* is provided (e.g. ``0o600`` for secrets), the file is created
    directly with that mode before any bytes are written.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    create_mode = 0o644 if mode is None else mode
    fd = -1
    tmp: Path | None = None
    for _ in range(16):
        cand = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        try:
            fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create_mode)
            tmp = cand
            break
        except FileExistsError:
            continue
    if tmp is None or fd < 0:
        raise OSError(f"could not allocate unique temp file for {path}")

    f = None
    try:
        f = os.fdopen(fd, "wb")
        fd = -1  # owned by file object now
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
        if mode is not None:
            os.fchmod(f.fileno(), mode)
        f.close()
        f = None
        os.replace(tmp, path)
    except Exception:
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
        elif fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: Path,
    text: str,
    mode: int | None = None,
    encoding: str = "utf-8",
) -> None:
    """Atomically write UTF-8 text to *path*."""
    atomic_write_bytes(path, text.encode(encoding), mode=mode)


def atomic_write_json(
    path: Path,
    obj: Any,
    mode: int | None = None,
    indent: int | None = 2,
) -> None:
    """Atomically serialize *obj* as JSON and write to *path*."""
    atomic_write_text(path, json.dumps(obj, indent=indent), mode=mode)


__all__ = [
    "AUDIO_EXTENSIONS",
    "LOSSLESS_EXTENSIONS",
    "LOSSLESS_FORMATS",
    "LOSSLESS_SQL",
    "PRIORITY_DEFAULT",
    "PRIORITY_MAX",
    "PRIORITY_MIN",
    "PRIORITY_OPERATOR",
    "PRIORITY_URGENT",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "blocklist_norm",
    "chunked",
    "container_to_host_path",
    "dir_leaf",
    "escape_like",
    "extract_match_tokens",
    "extract_rgid_from_key",
    "extract_year",
    "norm_album",
    "norm_artist",
    "normalize",
    "resolve_under",
    "tail_file",
    "tokens_match_path",
]

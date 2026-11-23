"""Centralized SQLite connection, schema initialization, RO handles, and backups.

Layer 1 — imports stdlib only.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wants (
    id INTEGER PRIMARY KEY,
    spotify_album_id TEXT UNIQUE NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    year INTEGER NOT NULL DEFAULT 0,
    track_count INTEGER,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'wanted',
    attempts INTEGER NOT NULL DEFAULT 0,
    search_misses INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts INTEGER NOT NULL DEFAULT 0,
    grabbed_ts INTEGER,
    grabbed_username TEXT,
    grabbed_dir TEXT,
    note TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 0,
    drop_window_until INTEGER NOT NULL DEFAULT 0,
    parked_prev_status TEXT NOT NULL DEFAULT '',
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wants_status ON wants(status);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    want_id INTEGER NOT NULL REFERENCES wants(id),
    username TEXT NOT NULL,
    dir TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_want ON attempts(want_id);

CREATE TABLE IF NOT EXISTS track_matches (
    spotify_track_id TEXT PRIMARY KEY,
    subsonic_song_id TEXT,
    matched_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artists (
    mbid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    followed_ts INTEGER NOT NULL,
    last_check_ts INTEGER NOT NULL DEFAULT 0,
    monitored INTEGER NOT NULL DEFAULT 1,
    want_types TEXT NOT NULL DEFAULT 'album,ep'
);

CREATE TABLE IF NOT EXISTS playlists (
    spotify_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    track_count INTEGER NOT NULL DEFAULT 0,
    hunt INTEGER NOT NULL DEFAULT 0,
    mirror INTEGER NOT NULL DEFAULT 1,
    last_seen_ts INTEGER NOT NULL,
    tracks_state TEXT NOT NULL DEFAULT 'pending',
    tracks_synced_ts INTEGER NOT NULL DEFAULT 0,
    owner TEXT NOT NULL DEFAULT '',
    gen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS passes (
    id INTEGER PRIMARY KEY,
    started_ts INTEGER NOT NULL,
    duration REAL NOT NULL,
    apply INTEGER NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1,
    stats TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS banned_peers (
    username TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT '',
    ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS peer_stats (
    username TEXT PRIMARY KEY,
    grabs INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    last_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS canon (
    rgid TEXT PRIMARY KEY,
    artist_mbid TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    year INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0,
    rtype TEXT NOT NULL DEFAULT '',
    updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_canon_artist ON canon(artist_mbid);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar (
    rgid TEXT PRIMARY KEY,
    artist_mbid TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    date TEXT NOT NULL,
    rtype TEXT NOT NULL DEFAULT '',
    updated_ts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sp_tracks (
    track_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    album TEXT NOT NULL,
    album_id TEXT NOT NULL DEFAULT '',
    year INTEGER NOT NULL DEFAULT 0,
    duration_s INTEGER NOT NULL DEFAULT 0,
    album_total INTEGER NOT NULL DEFAULT 0,
    liked_gen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sp_playlist_tracks (
    playlist_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    pos INTEGER NOT NULL DEFAULT 0,
    gen INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_sp_pl_tracks ON sp_playlist_tracks(playlist_id, pos);

CREATE TABLE IF NOT EXISTS sp_saved_albums (
    album_id TEXT PRIMARY KEY,
    artist TEXT NOT NULL,
    name TEXT NOT NULL,
    year INTEGER NOT NULL DEFAULT 0,
    total_tracks INTEGER NOT NULL DEFAULT 0,
    gen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sp_artists (
    artist_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    genres TEXT NOT NULL DEFAULT '',
    gen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'viewer',
    pw TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    last_login_ts INTEGER,
    disabled INTEGER NOT NULL DEFAULT 0,
    must_change_pw INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_ts INTEGER NOT NULL,
    last_seen_ts INTEGER NOT NULL,
    expires_ts INTEGER NOT NULL,
    remember INTEGER NOT NULL DEFAULT 0,
    ua TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    user TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    argv TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'run',
    runner TEXT NOT NULL,
    exec_id TEXT,
    pid INTEGER,
    log TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    started_ts INTEGER NOT NULL,
    finished_ts INTEGER,
    meta TEXT NOT NULL DEFAULT '{}',
    origin TEXT NOT NULL DEFAULT 'ui'
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_ts DESC);
"""

# Per-column idempotent migrations for existing hunter.db instances.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("wants", "search_misses", "INTEGER NOT NULL DEFAULT 0"),
    ("wants", "priority", "INTEGER NOT NULL DEFAULT 0"),
    ("wants", "drop_window_until", "INTEGER NOT NULL DEFAULT 0"),
    ("wants", "parked_prev_status", "TEXT NOT NULL DEFAULT ''"),
    ("artists", "monitored", "INTEGER NOT NULL DEFAULT 1"),
    ("artists", "want_types", "TEXT NOT NULL DEFAULT 'album,ep'"),
    ("playlists", "tracks_state", "TEXT NOT NULL DEFAULT 'pending'"),
    ("playlists", "tracks_synced_ts", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "owner", "TEXT NOT NULL DEFAULT ''"),
    ("playlists", "gen", "INTEGER NOT NULL DEFAULT 0"),
)


def _migrate_columns(conn: sqlite3.Connection) -> None:
    cols_by_table: dict[str, set[str]] = {}
    for table, col, col_def in _COLUMN_MIGRATIONS:
        if table not in cols_by_table:
            cols_by_table[table] = {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})")
            }
        if col not in cols_by_table[table]:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise
            cols_by_table[table].add(col)


def _connect_rw(path: Path, timeout: float) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=timeout)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn
    except Exception:
        conn.close()
        raise


def open_hunter_db(path: Path, timeout: float = 30.0) -> sqlite3.Connection:
    """Open ``hunter.db`` with WAL, foreign keys, busy_timeout, and one-time
    thread-safe DDL + per-column idempotent migrations per DB path per process."""
    path = Path(path)
    key = str(path.resolve())
    with _SCHEMA_LOCK:
        needs_init = (key not in _INITIALIZED) or (not path.exists())
        if needs_init:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = _connect_rw(path, timeout)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                _migrate_columns(conn)
                conn.commit()
            except Exception:
                conn.close()
                raise
            _INITIALIZED.add(key)
            return conn
    return _connect_rw(path, timeout)


def open_readonly_db(path: Path, timeout: float = 15.0) -> sqlite3.Connection:
    """Open an existing SQLite DB strictly read-only (``mode=ro``, ``query_only=ON``)."""
    abs_path = Path(os.path.abspath(os.fspath(path)))
    uri = abs_path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {max(0, int(timeout * 1000))}")
        conn.execute("PRAGMA query_only = ON")
        return conn
    except Exception:
        conn.close()
        raise


def backup_sqlite(src_path: Path, dst_path: Path, timeout: float = 30.0) -> Path:
    """Create an atomic consistent SQLite backup of *src_path* at *dst_path*."""
    src_path = Path(src_path)
    dst_path = Path(dst_path)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.parent / f".{dst_path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"

    src: sqlite3.Connection | None = None
    dst: sqlite3.Connection | None = None
    try:
        src = open_readonly_db(src_path, timeout=timeout)
        dst = sqlite3.connect(tmp, timeout=timeout)
        src.backup(dst)
    except Exception:
        if dst is not None:
            try:
                dst.close()
            except sqlite3.Error:
                pass
            dst = None
        if src is not None:
            try:
                src.close()
            except sqlite3.Error:
                pass
            src = None
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()

    os.replace(tmp, dst_path)
    return dst_path


def _is_backup_file(p: Path) -> bool:
    name = p.name
    return (
        (name.startswith("hunter_") and name.endswith(".db"))
        or (name.startswith("hunter.db.") and name.endswith(".manual"))
    )


def prune_backups(
    backup_dir: Path,
    keep_days: int = 14,
    *,
    keep_min: int = 1,
) -> int:
    """Prune daily and manual hunter.db backups older than *keep_days*,
    always preserving at least the *keep_min* newest snapshots."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return 0
    files: list[tuple[float, Path]] = []
    for p in backup_dir.iterdir():
        if not p.is_file() or not _is_backup_file(p):
            continue
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(key=lambda item: item[0], reverse=True)
    cutoff = time.time() - max(0, keep_days) * 86_400
    removed = 0
    for idx, (mtime, p) in enumerate(files):
        if idx < keep_min:
            continue
        if mtime < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def reset_schema_cache(path: Path | None = None) -> None:
    """Clear the process-wide initialized DB path cache (for tests)."""
    with _SCHEMA_LOCK:
        if path is None:
            _INITIALIZED.clear()
        else:
            _INITIALIZED.discard(str(Path(path).resolve()))


__all__ = [
    "backup_sqlite",
    "open_hunter_db",
    "open_readonly_db",
    "prune_backups",
    "reset_schema_cache",
]

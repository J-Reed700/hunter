"""Tests for hunter.db (schema, migrations, RO handles, backups, pruning)."""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hunter.db import (
    backup_sqlite,
    open_hunter_db,
    open_readonly_db,
    prune_backups,
    reset_schema_cache,
)


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "hunter.db"
        reset_schema_cache()

    def tearDown(self):
        reset_schema_cache()
        self.tmp.cleanup()

    def test_open_hunter_db_sets_pragmas_and_creates_all_tables(self):
        conn = open_hunter_db(self.db_path)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
            tables = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for expected in (
                "wants", "attempts", "track_matches", "artists", "playlists",
                "events", "passes", "banned_peers", "peer_stats", "canon",
                "meta", "calendar", "sp_tracks", "sp_playlist_tracks",
                "sp_saved_albums", "sp_artists", "users", "sessions", "audit", "jobs",
            ):
                self.assertIn(expected, tables)
            wcols = {r[1] for r in conn.execute("PRAGMA table_info(wants)")}
            self.assertIn("parked_prev_status", wcols)
        finally:
            conn.close()

    def test_idempotent_per_column_migration_on_legacy_db(self):
        legacy = sqlite3.connect(self.db_path)
        legacy.executescript("""
            CREATE TABLE wants (
                id INTEGER PRIMARY KEY,
                spotify_album_id TEXT UNIQUE NOT NULL,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                year INTEGER NOT NULL DEFAULT 0,
                track_count INTEGER,
                source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'wanted',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_ts INTEGER NOT NULL DEFAULT 0,
                grabbed_ts INTEGER,
                grabbed_username TEXT,
                grabbed_dir TEXT,
                note TEXT NOT NULL DEFAULT '',
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL
            );
            CREATE TABLE artists (
                mbid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                followed_ts INTEGER NOT NULL,
                last_check_ts INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE playlists (
                spotify_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                track_count INTEGER NOT NULL DEFAULT 0,
                hunt INTEGER NOT NULL DEFAULT 0,
                mirror INTEGER NOT NULL DEFAULT 1,
                last_seen_ts INTEGER NOT NULL
            );
            INSERT INTO wants (spotify_album_id, artist, album, source, created_ts, updated_ts)
            VALUES ('alb1', 'Artist', 'Album', 'saved_albums', 1, 1);
        """)
        legacy.commit()
        legacy.close()

        conn = open_hunter_db(self.db_path)
        try:
            row = conn.execute("SELECT * FROM wants WHERE spotify_album_id='alb1'").fetchone()
            self.assertEqual(row["search_misses"], 0)
            self.assertEqual(row["priority"], 0)
            self.assertEqual(row["drop_window_until"], 0)
            self.assertEqual(row["parked_prev_status"], "")
        finally:
            conn.close()

    def test_concurrent_open_is_thread_safe(self):
        errors: list[Exception] = []

        def worker():
            try:
                c = open_hunter_db(self.db_path)
                c.execute("SELECT COUNT(*) FROM wants").fetchone()
                c.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_open_readonly_db_blocks_writes(self):
        c = open_hunter_db(self.db_path)
        c.execute("INSERT INTO meta (key, value) VALUES ('k', 'v')")
        c.commit()
        c.close()

        ro = open_readonly_db(self.db_path)
        try:
            self.assertEqual(ro.execute("SELECT value FROM meta WHERE key='k'").fetchone()[0], "v")
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO meta (key, value) VALUES ('k2', 'v2')")
        finally:
            ro.close()

    def test_backup_sqlite_atomic_and_prune_backups(self):
        c = open_hunter_db(self.db_path)
        c.execute("INSERT INTO meta (key, value) VALUES ('hello', 'world')")
        c.commit()
        c.close()

        bdir = Path(self.tmp.name) / "backups"
        dst = bdir / "hunter_20261121.db"
        backup_sqlite(self.db_path, dst)
        ro = open_readonly_db(dst)
        try:
            self.assertEqual(ro.execute("SELECT value FROM meta WHERE key='hello'").fetchone()[0], "world")
        finally:
            ro.close()

        # Create old daily + manual backups and one fresh one
        old_daily = bdir / "hunter_20260101.db"
        old_manual = bdir / "hunter.db.20260101_000000.manual"
        old_daily.write_bytes(b"old")
        old_manual.write_bytes(b"old")
        old_ts = time.time() - 30 * 86_400
        os.utime(old_daily, (old_ts, old_ts))
        os.utime(old_manual, (old_ts, old_ts))

        pruned = prune_backups(bdir, keep_days=14)
        self.assertEqual(pruned, 2)
        self.assertTrue(dst.exists())
        self.assertFalse(old_daily.exists())
        self.assertFalse(old_manual.exists())


if __name__ == "__main__":
    unittest.main()

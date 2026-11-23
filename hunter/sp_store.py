"""Spotify library cache & Subsonic track-match persistence mixin for HunterState.

Layer 2 — operates on the SQLite connection owned by ``HunterState``.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from hunter import domain

_DEFAULT_HIT_TTL_S = 30 * 86_400
_DEFAULT_MISS_RETRY_S = 259_200


class SpotifyStoreMixin:
    """Cohesive persistence layer for ``playlists``, ``sp_*``, and ``track_matches``."""

    _conn: sqlite3.Connection

    def _commit(self) -> None: ...  # provided by HunterState
    def transaction(self) -> Any: ...  # provided by HunterState

    # --- playlists metadata ------------------------------------------------

    def playlists(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT spotify_id, name, track_count, hunt, mirror,"
            " tracks_state, tracks_synced_ts, owner"
            " FROM playlists ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def playlist_meta(self, spotify_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT spotify_id, name, track_count, tracks_state,"
            " tracks_synced_ts, hunt, mirror, owner"
            " FROM playlists WHERE spotify_id=?",
            (spotify_id,),
        ).fetchone()

    def playlist_flags(self) -> dict[str, tuple[bool, bool]]:
        return {
            r["spotify_id"]: (bool(r["hunt"]), bool(r["mirror"]))
            for r in self.playlists()
        }

    def set_playlist_flag(
        self, spotify_id: str, field: str, value: bool
    ) -> str | None:
        if field not in ("hunt", "mirror"):
            raise ValueError(field)
        self._conn.execute(
            f"UPDATE playlists SET {field}=? WHERE spotify_id=?",
            (1 if value else 0, spotify_id),
        )
        self._commit()
        row = self._conn.execute(
            "SELECT name FROM playlists WHERE spotify_id=?", (spotify_id,)
        ).fetchone()
        return row["name"] if row else None

    # --- spotify library cache (spsync engine) ------------------------------

    def sp_roster_for_sync(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT spotify_id, name, track_count, tracks_state"
            " FROM playlists"
            " ORDER BY hunt DESC, mirror DESC, name COLLATE NOCASE"
        ).fetchall()

    def sp_mark_playlist(self, spotify_id: str, gen: int, owner: str) -> None:
        self._conn.execute(
            "UPDATE playlists SET gen=?, owner=? WHERE spotify_id=?",
            (gen, owner, spotify_id),
        )
        self._commit()

    def _prune_orphan_sp_tracks(self) -> None:
        self._conn.execute(
            "DELETE FROM sp_tracks WHERE liked_gen = 0"
            " AND track_id NOT IN (SELECT track_id FROM sp_playlist_tracks)"
        )

    def sp_sweep_playlists(self, gen: int) -> int:
        with self.transaction():
            cur = self._conn.execute("DELETE FROM playlists WHERE gen != ?", (gen,))
            self._conn.execute(
                "DELETE FROM sp_playlist_tracks WHERE playlist_id NOT IN"
                " (SELECT spotify_id FROM playlists)"
            )
            self._prune_orphan_sp_tracks()
            return cur.rowcount

    def sp_delete_playlist(self, spotify_id: str) -> str | None:
        with self.transaction():
            row = self._conn.execute(
                "SELECT name FROM playlists WHERE spotify_id=?", (spotify_id,)
            ).fetchone()
            if not row:
                return None
            name = row["name"]
            self._conn.execute(
                "DELETE FROM sp_playlist_tracks WHERE playlist_id=?", (spotify_id,)
            )
            self._conn.execute(
                "DELETE FROM playlists WHERE spotify_id=?", (spotify_id,)
            )
            self._prune_orphan_sp_tracks()
            return name

    def sp_playlist_album_rows(self, playlist_id: str) -> list[sqlite3.Row]:
        """Return one deduplicated row per album in *playlist_id*, preferring
        persisted ``album_artist`` over track ``artist``."""
        return self._conn.execute(
            "SELECT t.album_id,"
            " COALESCE(NULLIF(MAX(t.album_artist), ''), MIN(t.artist)) AS artist,"
            " MIN(t.album) AS album,"
            " MAX(t.year) AS year,"
            " MAX(t.album_total) AS album_total"
            " FROM sp_playlist_tracks pt"
            " JOIN sp_tracks t ON t.track_id = pt.track_id"
            " WHERE pt.playlist_id=? AND t.album_id != ''"
            " GROUP BY t.album_id",
            (playlist_id,),
        ).fetchall()

    def sp_playlist_begin(self, playlist_id: str) -> None:
        self._conn.execute(
            "UPDATE playlists SET tracks_state="
            "  CASE WHEN tracks_state='ok' THEN 'ok' ELSE 'syncing' END"
            " WHERE spotify_id=?",
            (playlist_id,),
        )
        self._commit()

    def sp_upsert_track(
        self,
        track_id: str,
        title: str,
        artist: str,
        album: str,
        album_id: str,
        year: int,
        duration_s: int,
        album_total: int = 0,
        liked_gen: int | None = None,
        album_artist: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO sp_tracks (track_id, title, artist, album, album_artist,"
            " album_id, year, duration_s, album_total, liked_gen)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(track_id) DO UPDATE SET title=excluded.title,"
            " artist=excluded.artist, album=excluded.album,"
            " album_artist=CASE WHEN excluded.album_artist != '' THEN excluded.album_artist ELSE album_artist END,"
            " album_id=excluded.album_id, year=excluded.year,"
            " duration_s=excluded.duration_s,"
            " album_total=MAX(album_total, excluded.album_total)"
            + (", liked_gen=excluded.liked_gen" if liked_gen else ""),
            (
                track_id,
                title,
                artist,
                album,
                album_artist or "",
                album_id,
                year,
                duration_s,
                album_total,
                liked_gen or 0,
            ),
        )
        self._commit()

    def sp_link_track(
        self, playlist_id: str, track_id: str, pos: int, gen: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO sp_playlist_tracks (playlist_id, track_id, pos, gen)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(playlist_id, track_id) DO UPDATE SET"
            " pos=excluded.pos, gen=excluded.gen",
            (playlist_id, track_id, pos, gen),
        )
        self._commit()

    def sp_playlist_done(
        self, playlist_id: str, gen: int, track_count: int
    ) -> None:
        with self.transaction():
            self._conn.execute(
                "DELETE FROM sp_playlist_tracks WHERE playlist_id=? AND gen != ?",
                (playlist_id, gen),
            )
            self._conn.execute(
                "UPDATE playlists SET tracks_state='ok', tracks_synced_ts=?,"
                " track_count=? WHERE spotify_id=?",
                (int(time.time()), track_count, playlist_id),
            )
            self._prune_orphan_sp_tracks()

    def sp_playlist_track_rows(self, playlist_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT t.track_id, t.title, t.artist, t.album, t.album_artist,"
            " t.album_id, t.year, t.duration_s, t.album_total, t.liked_gen"
            " FROM sp_playlist_tracks pt JOIN sp_tracks t"
            " ON t.track_id = pt.track_id"
            " WHERE pt.playlist_id=? ORDER BY pt.pos",
            (playlist_id,),
        ).fetchall()

    def sp_album_track_rows(self, album_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT track_id, title, artist, album, album_artist, album_id,"
            " year, duration_s, album_total, liked_gen"
            " FROM sp_tracks WHERE album_id=? ORDER BY title",
            (album_id,),
        ).fetchall()

    def sp_cached_playlists(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT spotify_id, name, track_count FROM playlists"
            " WHERE tracks_state='ok' ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def sp_upsert_saved_album(
        self,
        album_id: str,
        artist: str,
        name: str,
        year: int,
        total_tracks: int,
        gen: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO sp_saved_albums (album_id, artist, name, year,"
            " total_tracks, gen) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(album_id) DO UPDATE SET artist=excluded.artist,"
            " name=excluded.name, year=excluded.year,"
            " total_tracks=excluded.total_tracks, gen=excluded.gen",
            (album_id, artist, name, year, total_tracks, gen),
        )
        self._commit()

    def sp_sweep_saved_albums(self, gen: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM sp_saved_albums WHERE gen != ?", (gen,)
        )
        self._commit()
        return cur.rowcount

    def sp_upsert_artist(
        self, artist_id: str, name: str, genres: str, gen: int
    ) -> None:
        self._conn.execute(
            "INSERT INTO sp_artists (artist_id, name, genres, gen)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(artist_id) DO UPDATE SET name=excluded.name,"
            " genres=excluded.genres, gen=excluded.gen",
            (artist_id, name, genres, gen),
        )
        self._commit()

    def sp_sweep_artists(self, gen: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM sp_artists WHERE gen != ?", (gen,)
        )
        self._commit()
        return cur.rowcount

    def sp_sweep_liked(self, gen: int) -> int:
        with self.transaction():
            cur = self._conn.execute(
                "UPDATE sp_tracks SET liked_gen=0"
                " WHERE liked_gen > 0 AND liked_gen != ?",
                (gen,),
            )
            self._prune_orphan_sp_tracks()
            return cur.rowcount

    # --- spotify cache read side (UI) ---------------------------------------

    _SP_ALBUM_SORTS = {
        "artist": "artist COLLATE NOCASE, name COLLATE NOCASE",
        "name": "name COLLATE NOCASE",
        "newest": "year DESC, name COLLATE NOCASE",
        "oldest": "year, name COLLATE NOCASE",
    }

    def sp_saved_album_rows(
        self, q: str, sort: str, limit: int, offset: int
    ) -> tuple[list[sqlite3.Row], int]:
        where, args = "", []
        if q:
            pat = f"%{domain.escape_like(q)}%"
            where = " WHERE artist LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\'"
            args = [pat, pat]
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM sp_saved_albums{where}", args
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT album_id, artist, name, year, total_tracks"
            f" FROM sp_saved_albums{where}"
            f" ORDER BY {self._SP_ALBUM_SORTS.get(sort, 'name COLLATE NOCASE')}"
            " LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        return rows, total

    def sp_liked_rows(
        self, q: str, limit: int, offset: int
    ) -> tuple[list[sqlite3.Row], int]:
        where, args = " WHERE liked_gen > 0", []
        if q:
            pat = f"%{domain.escape_like(q)}%"
            where += (
                " AND (title LIKE ? ESCAPE '\\' OR artist LIKE ? ESCAPE '\\'"
                " OR album LIKE ? ESCAPE '\\')"
            )
            args = [pat, pat, pat]
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM sp_tracks{where}", args
        ).fetchone()[0]
        rows = self._conn.execute(
            "SELECT track_id, title, artist, album, album_artist, album_id,"
            " year, duration_s, album_total"
            f" FROM sp_tracks{where}"
            " ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, title"
            " LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
        return rows, total

    def sp_artist_rows(self, q: str) -> list[sqlite3.Row]:
        if q:
            pat = f"%{domain.escape_like(q)}%"
            return self._conn.execute(
                "SELECT artist_id, name, genres FROM sp_artists"
                " WHERE name LIKE ? ESCAPE '\\'"
                " ORDER BY name COLLATE NOCASE LIMIT 300",
                (pat,),
            ).fetchall()
        return self._conn.execute(
            "SELECT artist_id, name, genres FROM sp_artists"
            " ORDER BY name COLLATE NOCASE LIMIT 300"
        ).fetchall()

    def sp_counts(self) -> dict[str, int]:
        row = self._conn.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM playlists),"
            " (SELECT COUNT(*) FROM playlists WHERE tracks_state='ok'),"
            " (SELECT COUNT(*) FROM sp_tracks),"
            " (SELECT COUNT(*) FROM sp_tracks WHERE liked_gen > 0),"
            " (SELECT COUNT(*) FROM sp_saved_albums),"
            " (SELECT COUNT(*) FROM sp_artists)"
        ).fetchone()
        return {
            "playlists": row[0],
            "playlists_synced": row[1],
            "tracks": row[2],
            "liked": row[3],
            "saved_albums": row[4],
            "artists": row[5],
        }

    # --- playlist match cache ---------------------------------------------

    def get_track_match(
        self,
        spotify_track_id: str,
        *,
        hit_ttl_s: int = _DEFAULT_HIT_TTL_S,
        miss_retry_s: int = _DEFAULT_MISS_RETRY_S,
    ) -> tuple[bool, str | None]:
        row = self._conn.execute(
            "SELECT subsonic_song_id, matched_ts FROM track_matches"
            " WHERE spotify_track_id=?",
            (spotify_track_id,),
        ).fetchone()
        if row is None:
            return False, None
        age = time.time() - row["matched_ts"]
        if row["subsonic_song_id"] is not None:
            if hit_ttl_s <= 0 or age < hit_ttl_s:
                return True, row["subsonic_song_id"]
            return False, None
        if age < miss_retry_s:
            return True, None
        return False, None

    def set_track_match(
        self, spotify_track_id: str, song_id: str | None
    ) -> None:
        self._conn.execute(
            "INSERT INTO track_matches (spotify_track_id, subsonic_song_id, matched_ts)"
            " VALUES (?,?,?)"
            " ON CONFLICT(spotify_track_id) DO UPDATE SET"
            " subsonic_song_id=excluded.subsonic_song_id,"
            " matched_ts=excluded.matched_ts",
            (spotify_track_id, song_id, int(time.time())),
        )
        self._commit()

    def invalidate_track_match(self, spotify_track_id: str) -> None:
        self._conn.execute(
            "DELETE FROM track_matches WHERE spotify_track_id=?",
            (spotify_track_id,),
        )
        self._commit()


__all__ = ["SpotifyStoreMixin"]

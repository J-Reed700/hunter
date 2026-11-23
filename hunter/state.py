"""Hunter state DB — wants lifecycle + attempt history + playlist match cache.

SQLite/WAL via ``hunter.db.open_hunter_db``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hunter import domain
from hunter.db import open_hunter_db

MISS_RETRY_SECONDS = 259_200   # re-search Subsonic misses every 3 days
_DEFAULT_HIT_TTL_S = 30 * 86_400
_PASS_STATS_MAX_BYTES = 8192
_OPERATOR_EVENT_KINDS = "('automation','request','reseed','release','follow')"
_UNSET = object()


@dataclass(frozen=True)
class Want:
    id: int
    spotify_album_id: str
    artist: str
    album: str
    year: int
    track_count: int | None
    source: str
    status: str
    attempts: int               # real grabs sent to slskd
    search_misses: int          # passes that found no viable candidate
    next_attempt_ts: int
    grabbed_ts: int | None
    grabbed_username: str | None
    grabbed_dir: str | None
    note: str
    priority: int
    drop_window_until: int
    created_ts: int
    updated_ts: int


def _row_to_want(row: sqlite3.Row) -> Want:
    return Want(
        id=row["id"],
        spotify_album_id=row["spotify_album_id"],
        artist=row["artist"],
        album=row["album"],
        year=row["year"],
        track_count=row["track_count"],
        source=row["source"],
        status=row["status"],
        attempts=row["attempts"],
        search_misses=row["search_misses"],
        next_attempt_ts=row["next_attempt_ts"],
        grabbed_ts=row["grabbed_ts"],
        grabbed_username=row["grabbed_username"],
        grabbed_dir=row["grabbed_dir"],
        note=row["note"],
        priority=row["priority"],
        drop_window_until=row["drop_window_until"],
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
    )


def _compact_pass_stats(stats_json: str) -> str:
    """Keep pass stats strictly valid JSON and <= 8 KB."""
    if len(stats_json.encode("utf-8", errors="replace")) <= _PASS_STATS_MAX_BYTES:
        try:
            json.loads(stats_json)
            return stats_json
        except ValueError:
            return json.dumps({"raw": stats_json[:2000]}, separators=(",", ":"))
    try:
        parsed = json.loads(stats_json)
    except ValueError:
        return json.dumps({"raw": stats_json[:2000]}, separators=(",", ":"))
    if not isinstance(parsed, dict):
        return json.dumps({"_truncated": True}, separators=(",", ":"))

    trimmed = dict(parsed)
    trimmed["_truncated"] = True
    compact = json.dumps(trimmed, separators=(",", ":"))
    if len(compact.encode("utf-8")) <= _PASS_STATS_MAX_BYTES:
        return compact

    # Drop largest non-scalar / bulky keys until within budget
    for k, _v in sorted(
        list(trimmed.items()),
        key=lambda kv: len(json.dumps(kv[1], default=str)),
        reverse=True,
    ):
        if k == "_truncated":
            continue
        trimmed.pop(k, None)
        compact = json.dumps(trimmed, separators=(",", ":"))
        if len(compact.encode("utf-8")) <= _PASS_STATS_MAX_BYTES:
            return compact
    return json.dumps({"_truncated": True}, separators=(",", ":"))


class HunterState:
    def __init__(self, db_path: Path):
        self._conn = open_hunter_db(db_path)
        self._tx_depth = 0

    def __enter__(self) -> "HunterState":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Batch multiple mutator calls into a single SQLite transaction."""
        if self._tx_depth == 0:
            if not self._conn.in_transaction:
                self._conn.execute("BEGIN")
            self._tx_depth += 1
            try:
                yield
            except Exception:
                self._tx_depth -= 1
                self._conn.rollback()
                raise
            else:
                self._tx_depth -= 1
                self._conn.commit()
        else:
            sp = f"sp_{self._tx_depth}"
            self._conn.execute(f"SAVEPOINT {sp}")
            self._tx_depth += 1
            try:
                yield
            except Exception:
                self._tx_depth -= 1
                self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                self._conn.execute(f"RELEASE SAVEPOINT {sp}")
                raise
            else:
                self._tx_depth -= 1
                self._conn.execute(f"RELEASE SAVEPOINT {sp}")

    # --- wants -------------------------------------------------------------

    def upsert_want(
        self,
        spotify_album_id: str,
        artist: str,
        album: str,
        year: int,
        track_count: int | None,
        source: str,
        resurrect: bool = False,
    ) -> bool:
        """Insert if unseen. Returns True when this is a NEW want
        (or a resurrected skipped/exhausted row when resurrect=True)."""
        blocked = False
        try:
            from hunter import blocklist as _bl
            blocked = _bl.is_blocked(artist)
        except OSError:
            blocked = False

        if blocked:
            if not self.has_want(spotify_album_id):
                self.log_event(
                    "blocked",
                    artist,
                    album,
                    f"{source}: on the artist blocklist — not wanted",
                )
            return False

        now = int(time.time())
        cur = self._conn.execute(
            "INSERT INTO wants (spotify_album_id, artist, album, year,"
            " track_count, source, created_ts, updated_ts)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(spotify_album_id) DO NOTHING",
            (spotify_album_id, artist, album, year, track_count, source, now, now),
        )
        if cur.rowcount > 0:
            self._commit()
            return True

        if track_count:
            self._conn.execute(
                "UPDATE wants SET track_count=? WHERE spotify_album_id=?"
                " AND track_count IS NULL",
                (track_count, spotify_album_id),
            )
        if resurrect:
            cur = self._conn.execute(
                "UPDATE wants SET status='wanted', attempts=0,"
                " search_misses=0, next_attempt_ts=0,"
                " grabbed_ts=NULL, grabbed_username=NULL, grabbed_dir=NULL,"
                " drop_window_until=0, note='re-requested', updated_ts=?"
                " WHERE spotify_album_id=? AND status IN ('skipped','exhausted')",
                (now, spotify_album_id),
            )
            self._commit()
            return cur.rowcount > 0
        self._commit()
        return False

    def update_track_count(
        self, spotify_album_id: str, track_count: int | None
    ) -> None:
        if track_count:
            self._conn.execute(
                "UPDATE wants SET track_count=? WHERE spotify_album_id=?"
                " AND track_count IS NULL",
                (track_count, spotify_album_id),
            )
            self._commit()

    def get_want(self, want_id: int) -> Want | None:
        row = self._conn.execute(
            "SELECT * FROM wants WHERE id=?", (want_id,)
        ).fetchone()
        return _row_to_want(row) if row else None

    def want_by_key(self, spotify_album_id: str) -> Want | None:
        row = self._conn.execute(
            "SELECT * FROM wants WHERE spotify_album_id=?",
            (spotify_album_id,),
        ).fetchone()
        return _row_to_want(row) if row else None

    def want_row(self, want_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM wants WHERE id=?", (want_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_want_fields(
        self,
        want_id: int,
        *,
        artist: str | None = None,
        album: str | None = None,
        track_count: Any = _UNSET,
        year: int | None = None,
    ) -> bool:
        sets: list[str] = []
        args: list[Any] = []
        if artist is not None and artist:
            sets.append("artist=?")
            args.append(artist)
        if album is not None and album:
            sets.append("album=?")
            args.append(album)
        if track_count is not _UNSET:
            sets.append("track_count=?")
            args.append(track_count)
        if year is not None:
            sets.append("year=?")
            args.append(year)
        if not sets:
            return False
        sets.append("updated_ts=?")
        args.extend([int(time.time()), want_id])
        cur = self._conn.execute(
            f"UPDATE wants SET {', '.join(sets)} WHERE id=?", args
        )
        self._commit()
        return cur.rowcount > 0

    def all_open(self) -> list[Want]:
        rows = self._conn.execute(
            "SELECT * FROM wants WHERE status IN ('wanted','grabbed')"
        ).fetchall()
        return [_row_to_want(r) for r in rows]

    def eligible_for_hunt(self, limit: int) -> list[Want]:
        now = int(time.time())
        rows = self._conn.execute(
            "SELECT * FROM wants WHERE status='wanted'"
            " AND (next_attempt_ts <= ? OR drop_window_until > ?)"
            " ORDER BY CASE WHEN drop_window_until > ? THEN -1"
            "               WHEN source IN ('request','manual','drop') THEN 0"
            "               WHEN source='upgrade' THEN 3"
            "               WHEN source='gapfill' THEN 2"
            "               ELSE 1 END,"
            " priority, created_ts LIMIT ?",
            (now, now, now, limit),
        ).fetchall()
        return [_row_to_want(r) for r in rows]

    def set_priority(self, spotify_album_id: str, priority: int) -> bool:
        cur = self._conn.execute(
            "UPDATE wants SET priority=? WHERE spotify_album_id=?",
            (priority, spotify_album_id),
        )
        self._commit()
        return cur.rowcount > 0

    def set_priority_by_id(self, want_id: int, priority: int) -> bool:
        cur = self._conn.execute(
            "UPDATE wants SET priority=? WHERE id=?",
            (priority, want_id),
        )
        self._commit()
        return cur.rowcount > 0

    def bulk_set_priority(self, pairs: Iterable[tuple[int, int]]) -> int:
        """Update priorities for ``(want_id, priority)`` pairs where changed."""
        changed = 0
        with self.transaction():
            for want_id, prio in pairs:
                cur = self._conn.execute(
                    "UPDATE wants SET priority=? WHERE id=? AND priority != ?",
                    (prio, want_id, prio),
                )
                changed += cur.rowcount
        return changed

    def open_drop_window(self, want_id: int, seconds: int) -> None:
        self._conn.execute(
            "UPDATE wants SET drop_window_until=?, next_attempt_ts=0 WHERE id=?",
            (int(time.time()) + seconds, want_id),
        )
        self._commit()

    # --- peer reputation ----------------------------------------------------

    def record_peer_grab(self, username: str) -> None:
        if not username or username.startswith("prowlarr:"):
            return
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO peer_stats (username, grabs, last_ts) VALUES (?,1,?)"
            " ON CONFLICT(username) DO UPDATE SET grabs=grabs+1, last_ts=?",
            (username, now, now),
        )
        self._commit()

    def record_peer_outcome(self, username: str, ok: bool) -> None:
        if not username or username.startswith("prowlarr:"):
            return
        col = "completed" if ok else "failed"
        now = int(time.time())
        self._conn.execute(
            f"INSERT INTO peer_stats (username, {col}, last_ts) VALUES (?,1,?)"
            f" ON CONFLICT(username) DO UPDATE SET {col}={col}+1, last_ts=?",
            (username, now, now),
        )
        self._commit()

    def peer_reputation(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self._conn.execute(
            "SELECT username, completed, failed FROM peer_stats"
            " WHERE completed+failed > 0"
        ):
            c, f = r[1], r[2]
            out[r[0]] = (c - f) / (c + f + 2.0)
        return out

    def peers_to_autoban(self, strikes: int) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT username FROM peer_stats"
                " WHERE failed >= ? AND completed = 0",
                (strikes,),
            )
        ]

    def peer_stat_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT username, grabs, completed, failed,"
            " datetime(last_ts,'unixepoch','localtime') AS at"
            " FROM peer_stats WHERE grabs > 0 OR failed > 0"
            " ORDER BY completed DESC, failed ASC LIMIT 100"
        ).fetchall()

    # --- canon (essential-albums scoring) ----------------------------------

    def upsert_canon(
        self,
        rgid: str,
        artist_mbid: str,
        artist: str,
        album: str,
        year: int,
        score: float,
        rtype: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO canon (rgid, artist_mbid, artist, album, year, score,"
            " rtype, updated_ts) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(rgid) DO UPDATE SET score=excluded.score,"
            " album=excluded.album, year=excluded.year, updated_ts=excluded.updated_ts",
            (rgid, artist_mbid, artist, album, year, score, rtype, int(time.time())),
        )
        self._commit()

    def canon_for_artist(self, artist_mbid: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT rgid, artist, album, year, score, rtype FROM canon"
            " WHERE artist_mbid=? ORDER BY score DESC",
            (artist_mbid,),
        ).fetchall()

    def canon_age(self, artist_mbid: str) -> float:
        row = self._conn.execute(
            "SELECT MAX(updated_ts) FROM canon WHERE artist_mbid=?",
            (artist_mbid,),
        ).fetchone()
        return time.time() - row[0] if row and row[0] else 1e12

    # --- events ------------------------------------------------------------

    def log_event(
        self,
        kind: str,
        artist: str = "",
        album: str = "",
        detail: str = "",
    ) -> None:
        cur = self._conn.execute(
            "INSERT INTO events (ts, kind, artist, album, detail)"
            " VALUES (?,?,?,?,?)",
            (int(time.time()), kind, artist, album, detail[:300]),
        )
        if (cur.lastrowid or 0) % 50 == 0:
            self._conn.execute(
                f"DELETE FROM events WHERE kind NOT IN {_OPERATOR_EVENT_KINDS}"
                f" AND id NOT IN (SELECT id FROM events WHERE kind NOT IN"
                f" {_OPERATOR_EVENT_KINDS} ORDER BY id DESC LIMIT 2000)"
            )
        self._commit()

    def events(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        kind: str = "",
    ) -> list[sqlite3.Row]:
        if kind:
            return self._conn.execute(
                "SELECT kind, artist, album, detail,"
                " datetime(ts,'unixepoch','localtime') AS at"
                " FROM events WHERE kind=? ORDER BY ts DESC, id DESC"
                " LIMIT ? OFFSET ?",
                (kind, limit, offset),
            ).fetchall()
        return self._conn.execute(
            "SELECT kind, artist, album, detail,"
            " datetime(ts,'unixepoch','localtime') AS at"
            " FROM events ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    def event_kinds(self) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT kind FROM events GROUP BY kind ORDER BY COUNT(*) DESC"
            )
        ]

    def want_events(
        self, artist: str, album: str, limit: int = 30
    ) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE artist=? AND album=?"
            " ORDER BY ts DESC, id DESC LIMIT ?",
            (artist, album, limit),
        ).fetchall()

    def set_status(self, want_id: int, status: str, note: str = "") -> None:
        row = self._conn.execute(
            "SELECT artist, album FROM wants WHERE id=?", (want_id,)
        ).fetchone()
        self._conn.execute(
            "UPDATE wants SET status=?, note=?, updated_ts=? WHERE id=?",
            (status, note, int(time.time()), want_id),
        )
        self._commit()
        if row and status in ("done", "exhausted", "skipped"):
            self.log_event(status, row["artist"], row["album"], note)

    def mark_grabbed(self, want_id: int, username: str, dir_: str) -> None:
        now = int(time.time())
        row = self._conn.execute(
            "SELECT artist, album FROM wants WHERE id=?", (want_id,)
        ).fetchone()
        self._conn.execute(
            "UPDATE wants SET status='grabbed', attempts=attempts+1,"
            " grabbed_ts=?, grabbed_username=?, grabbed_dir=?, updated_ts=?"
            " WHERE id=?",
            (now, username, dir_, now, want_id),
        )
        self._conn.execute(
            "INSERT INTO attempts (want_id, username, dir, ts) VALUES (?,?,?,?)",
            (want_id, username, dir_, now),
        )
        self._commit()
        if row:
            self.log_event("grab", row["artist"], row["album"], f"peer {username}")

    def revert_to_wanted(self, want_id: int, backoff_seconds: int) -> None:
        now = int(time.time())
        self._conn.execute(
            "UPDATE wants SET status='wanted', next_attempt_ts=?, updated_ts=?,"
            " grabbed_ts=NULL, grabbed_username=NULL, grabbed_dir=NULL"
            " WHERE id=?",
            (now + backoff_seconds, now, want_id),
        )
        self._commit()

    def bump_backoff(
        self,
        want_id: int,
        backoff_seconds: int,
        count_attempt: bool = True,
    ) -> None:
        now = int(time.time())
        self._conn.execute(
            "UPDATE wants SET search_misses=search_misses+?,"
            " next_attempt_ts=?, updated_ts=? WHERE id=?",
            (1 if count_attempt else 0, now + backoff_seconds, now, want_id),
        )
        self._commit()

    def tried_candidates(self, want_id: int) -> set[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT username, dir FROM attempts WHERE want_id=?", (want_id,)
        ).fetchall()
        return {(r["username"], r["dir"]) for r in rows}

    def note_tried(self, want_id: int, username: str, dir_: str) -> None:
        self._conn.execute(
            "INSERT INTO attempts (want_id, username, dir, ts) VALUES (?,?,?,?)",
            (want_id, username, dir_, int(time.time())),
        )
        self._commit()

    def attempt_history(self, want_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT username, dir,"
            " datetime(ts,'unixepoch','localtime') AS at"
            " FROM attempts WHERE want_id=? ORDER BY ts DESC LIMIT 20",
            (want_id,),
        ).fetchall()

    def retry_now(self, want_id: int) -> bool:
        cur = self._conn.execute(
            "UPDATE wants SET status='wanted', next_attempt_ts=0,"
            " attempts=CASE WHEN status='exhausted' THEN 0 ELSE attempts END,"
            " search_misses=CASE WHEN status='exhausted' THEN 0"
            "                    ELSE search_misses END,"
            " grabbed_ts=NULL, grabbed_username=NULL, grabbed_dir=NULL,"
            " parked_prev_status='', note='retry forced via ui', updated_ts=?"
            " WHERE id=? AND status IN ('wanted','grabbed','exhausted','skipped')",
            (int(time.time()), want_id),
        )
        self._commit()
        return cur.rowcount > 0

    # --- peer blocklist -----------------------------------------------------

    def ban_peer(self, username: str, reason: str = "") -> bool:
        cur = self._conn.execute(
            "INSERT INTO banned_peers (username, reason, ts) VALUES (?,?,?)"
            " ON CONFLICT(username) DO NOTHING",
            (username, reason[:200], int(time.time())),
        )
        self._commit()
        return cur.rowcount > 0

    def unban_peer(self, username: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM banned_peers WHERE username=?", (username,)
        )
        self._commit()
        return cur.rowcount > 0

    def banned_peers(self) -> set[str]:
        return {
            r[0]
            for r in self._conn.execute("SELECT username FROM banned_peers")
        }

    def banned_peer_rows(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT username, reason,"
            " datetime(ts,'unixepoch','localtime') AS at"
            " FROM banned_peers ORDER BY ts DESC"
        ).fetchall()

    # --- meta kv ------------------------------------------------------------

    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._commit()

    # --- pass history -------------------------------------------------------

    def record_pass(
        self,
        started_ts: int,
        duration: float,
        apply: bool,
        ok: bool,
        stats_json: str,
    ) -> None:
        safe_stats = _compact_pass_stats(stats_json)
        self._conn.execute(
            "INSERT INTO passes (started_ts, duration, apply, ok, stats)"
            " VALUES (?,?,?,?,?)",
            (started_ts, round(duration, 1), int(apply), int(ok), safe_stats),
        )
        self._conn.execute(
            "DELETE FROM passes WHERE id NOT IN"
            " (SELECT id FROM passes ORDER BY started_ts DESC, id DESC LIMIT 500)"
        )
        self._commit()

    def pass_history(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT started_ts, duration, apply, ok, stats,"
            " datetime(started_ts,'unixepoch','localtime') AS at"
            " FROM passes ORDER BY started_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def recent_passes_parsed(self, limit: int = 30) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.pass_history(limit):
            d = dict(r)
            try:
                d["stats"] = json.loads(d.get("stats") or "{}")
            except (TypeError, ValueError):
                d["stats"] = {}
            out.append(d)
        return out

    def want_id_by_key(self, spotify_album_id: str) -> int | None:
        row = self._conn.execute(
            "SELECT id FROM wants WHERE spotify_album_id=?",
            (spotify_album_id,),
        ).fetchone()
        return row[0] if row else None

    def has_want(self, spotify_album_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM wants WHERE spotify_album_id=? LIMIT 1",
                (spotify_album_id,),
            ).fetchone()
            is not None
        )

    def source_exists(self, source: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM wants WHERE source=? LIMIT 1", (source,)
            ).fetchone()
            is not None
        )

    def source_active(self, source: str) -> bool:
        """True when *source* has at least one non-skipped want."""
        return (
            self._conn.execute(
                "SELECT 1 FROM wants WHERE source=? AND status != 'skipped' LIMIT 1",
                (source,),
            ).fetchone()
            is not None
        )

    def source_counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for source, status, n in self._conn.execute(
            "SELECT source, status, COUNT(*) FROM wants GROUP BY source, status"
        ):
            group = (
                "playlist"
                if source.startswith("playlist:")
                else "release"
                if source.startswith("release:")
                else source
            )
            out.setdefault(group, {})[status] = out.get(group, {}).get(status, 0) + n
        return out

    def count_by_source(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for source, status, n in self._conn.execute(
            "SELECT source, status, COUNT(*) FROM wants GROUP BY source, status"
        ):
            out.setdefault(source, {})[status] = n
        return out

    def spotify_want_counts(self) -> dict[str, int]:
        return {
            r[0]: r[1]
            for r in self._conn.execute(
                "SELECT status, COUNT(*) FROM wants"
                " WHERE source IN ('saved', 'saved_albums') OR source LIKE 'playlist:%'"
                " GROUP BY status"
            )
        }

    def playlist_want_counts(self) -> dict[str, dict[str, int]]:
        per: dict[str, dict[str, int]] = {}
        for src, status, n in self._conn.execute(
            "SELECT source, status, COUNT(*) FROM wants"
            " WHERE source LIKE 'playlist:%' GROUP BY source, status"
        ):
            per.setdefault(src[9:], {})[status] = n
        return per

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM wants GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def open_wants_by_artist(self) -> dict[str, int]:
        return {
            r[0]: r[1]
            for r in self._conn.execute(
                "SELECT artist, COUNT(*) FROM wants"
                " WHERE status IN ('wanted','grabbed') GROUP BY artist"
            )
        }

    def open_wants_count_for_artists(self, artists: list[str]) -> int:
        if not artists:
            return 0
        total = 0
        for batch in domain.chunked(artists, 400):
            ph = ",".join("?" * len(batch))
            total += self._conn.execute(
                f"SELECT COUNT(*) FROM wants"
                f" WHERE status IN ('wanted','grabbed','exhausted')"
                f" AND artist IN ({ph})",
                batch,
            ).fetchone()[0]
        return total

    def open_wants_count_for_keys(self, keys: list[str]) -> int:
        if not keys:
            return 0
        total = 0
        for batch in domain.chunked(keys, 400):
            ph = ",".join("?" * len(batch))
            total += self._conn.execute(
                f"SELECT COUNT(*) FROM wants"
                f" WHERE status IN ('wanted','grabbed','exhausted')"
                f" AND spotify_album_id IN ({ph})",
                batch,
            ).fetchone()[0]
        return total

    def wants_for_artists_and_keys(
        self, artists: list[str], keys: list[str]
    ) -> list[Want]:
        seen_ids: set[int] = set()
        out: list[Want] = []
        for batch in domain.chunked(artists, 400):
            ph = ",".join("?" * len(batch))
            for r in self._conn.execute(
                f"SELECT * FROM wants WHERE status IN ('wanted','grabbed','exhausted')"
                f" AND artist IN ({ph})",
                batch,
            ):
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    out.append(_row_to_want(r))
        for batch in domain.chunked(keys, 400):
            ph = ",".join("?" * len(batch))
            for r in self._conn.execute(
                f"SELECT * FROM wants WHERE status IN ('wanted','grabbed','exhausted')"
                f" AND spotify_album_id IN ({ph})",
                batch,
            ):
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    out.append(_row_to_want(r))
        return out

    def wants_status_map(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        clean = [k for k in keys if k]
        for batch in domain.chunked(clean, 400):
            ph = ",".join("?" * len(batch))
            for r in self._conn.execute(
                f"SELECT id, spotify_album_id, status, attempts FROM wants"
                f" WHERE spotify_album_id IN ({ph})",
                batch,
            ):
                out[r["spotify_album_id"]] = {
                    "id": r["id"],
                    "status": r["status"],
                    "attempts": r["attempts"],
                }
        return out

    def drop_wants(
        self,
        artist_like: str,
        album_like: str,
        source_like: str = "%",
        *,
        literal: bool = False,
    ) -> int:
        """Mark matching open/exhausted wants as skipped.

        When *literal* is True, escapes LIKE metacharacters so underscores/percent
        signs match literally; default ``False`` preserves CLI wildcard support.
        """
        if literal:
            clauses = ["status IN ('wanted','grabbed','exhausted')"]
            args: list[Any] = [int(time.time())]
            if artist_like != "%":
                clauses.append("artist LIKE ? ESCAPE '\\'")
                args.append(domain.escape_like(artist_like))
            if album_like != "%":
                clauses.append("album LIKE ? ESCAPE '\\'")
                args.append(domain.escape_like(album_like))
            if source_like and source_like != "%":
                clauses.append("source LIKE ? ESCAPE '\\'")
                args.append(domain.escape_like(source_like))
            where = " AND ".join(clauses)
            cur = self._conn.execute(
                f"UPDATE wants SET status='skipped', updated_ts=? WHERE {where}",
                args,
            )
        else:
            cur = self._conn.execute(
                "UPDATE wants SET status='skipped', updated_ts=?"
                " WHERE status IN ('wanted','grabbed','exhausted')"
                " AND artist LIKE ? AND album LIKE ? AND source LIKE ?",
                (int(time.time()), artist_like, album_like, source_like),
            )
        self._commit()
        return cur.rowcount

    def drop_wants_for_blocked(
        self, norm_keys: set[str], note: str = "blocked"
    ) -> int:
        """Skip open/exhausted wants whose normalized artist matches *norm_keys*."""
        if not norm_keys:
            return 0
        rows = self._conn.execute(
            "SELECT id, artist FROM wants"
            " WHERE status IN ('wanted','grabbed','exhausted')"
        ).fetchall()
        ids = [
            r["id"]
            for r in rows
            if domain.blocklist_norm(r["artist"]) in norm_keys
        ]
        if not ids:
            return 0
        now = int(time.time())
        dropped = 0
        for batch in domain.chunked(ids, 400):
            ph = ",".join("?" * len(batch))
            cur = self._conn.execute(
                f"UPDATE wants SET status='skipped', updated_ts=?, note=?"
                f" WHERE id IN ({ph})",
                [now, note, *batch],
            )
            dropped += cur.rowcount
        self._commit()
        return dropped

    def park_wants_by_keys(self, keys: list[str], note: str = "") -> int:
        if not keys:
            return 0
        n = 0
        now = int(time.time())
        for chunk in domain.chunked(keys, 400):
            cur = self._conn.execute(
                f"UPDATE wants SET parked_prev_status=status, status='skipped',"
                f" updated_ts=?, note=?"
                f" WHERE status IN ('wanted','grabbed','exhausted')"
                f" AND spotify_album_id IN ({','.join('?' * len(chunk))})",
                [now, note, *chunk],
            )
            n += cur.rowcount
        self._commit()
        return n

    def park_wants(self, artists: list[str], keys: list[str], note: str) -> int:
        """RESERVE step of a removal: record prior status and skip open wants."""
        now = int(time.time())
        n = 0
        for name in artists:
            cur = self._conn.execute(
                "UPDATE wants SET parked_prev_status=status, status='skipped',"
                " updated_ts=?, note=?"
                " WHERE status IN ('wanted','grabbed','exhausted') AND artist = ?",
                (now, note, name),
            )
            n += cur.rowcount
        for chunk in domain.chunked(keys, 400):
            cur = self._conn.execute(
                f"UPDATE wants SET parked_prev_status=status, status='skipped',"
                f" updated_ts=?, note=?"
                f" WHERE status IN ('wanted','grabbed','exhausted')"
                f" AND spotify_album_id IN ({','.join('?' * len(chunk))})",
                [now, note, *chunk],
            )
            n += cur.rowcount
        self._commit()
        return n

    def restore_parked(self, note: str) -> int:
        """COMPENSATE step: restore each parked row to its exact prior status."""
        cur = self._conn.execute(
            "UPDATE wants"
            " SET status = CASE WHEN parked_prev_status != '' THEN parked_prev_status ELSE 'wanted' END,"
            "     parked_prev_status = '',"
            "     updated_ts = ?,"
            "     note = ''"
            " WHERE status = 'skipped' AND note = ?",
            (int(time.time()), note),
        )
        self._commit()
        return cur.rowcount

    def recent_open(self, limit: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT status, artist, album, source, attempts,"
            " datetime(updated_ts,'unixepoch','localtime') AS upd"
            " FROM wants WHERE status != 'done'"
            " ORDER BY updated_ts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    _SORTS = {
        "updated": "updated_ts DESC",
        "created": "created_ts DESC",
        "artist": "artist COLLATE NOCASE, album COLLATE NOCASE",
        "attempts": "attempts DESC, updated_ts DESC",
        "status": "status, updated_ts DESC",
    }

    @staticmethod
    def _wants_where(
        statuses: list[str] | tuple[str, ...], source: str, text: str
    ) -> tuple[str, list[Any]]:
        where: list[str] = []
        args: list[Any] = []
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        if source:
            where.append("source LIKE ? ESCAPE '\\'")
            args.append(domain.escape_like(source) + "%")
        if text:
            pat = f"%{domain.escape_like(text)}%"
            where.append("(artist LIKE ? ESCAPE '\\' OR album LIKE ? ESCAPE '\\')")
            args.extend([pat, pat])
        return (" WHERE " + " AND ".join(where)) if where else "", args

    def wants_query(
        self,
        *,
        statuses: list[str],
        source: str,
        text: str,
        sort: str,
        limit: int,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        wsql, args = self._wants_where(statuses, source, text)
        sql = (
            "SELECT id, spotify_album_id, status, artist, album, year, track_count, source,"
            " attempts, search_misses, note, grabbed_username, grabbed_dir,"
            " datetime(created_ts,'unixepoch','localtime') AS created,"
            " datetime(updated_ts,'unixepoch','localtime') AS upd"
            f" FROM wants{wsql}"
            f" ORDER BY {self._SORTS.get(sort, self._SORTS['updated'])}"
            " LIMIT ? OFFSET ?"
        )
        return self._conn.execute(sql, [*args, limit, offset]).fetchall()

    def wants_count_filtered(
        self,
        *,
        statuses: list[str] | tuple[str, ...] = (),
        source: str = "",
        text: str = "",
    ) -> int:
        wsql, args = self._wants_where(statuses, source, text)
        return self._conn.execute(
            f"SELECT COUNT(*) FROM wants{wsql}", args
        ).fetchone()[0]

    def want_sources(self) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT CASE WHEN source LIKE 'playlist:%' THEN 'playlist'"
                " WHEN source LIKE 'release:%' THEN 'release' ELSE source END AS s"
                " FROM wants ORDER BY s"
            )
        ]

    # --- followed artists --------------------------------------------------

    def follow_artist(self, mbid: str, name: str) -> bool:
        cur = self._conn.execute(
            "INSERT INTO artists (mbid, name, followed_ts) VALUES (?,?,?)"
            " ON CONFLICT(mbid) DO NOTHING",
            (mbid, name, int(time.time())),
        )
        self._commit()
        return cur.rowcount > 0

    def unfollow_artist(self, mbid: str) -> bool:
        cur = self._conn.execute("DELETE FROM artists WHERE mbid=?", (mbid,))
        self._commit()
        return cur.rowcount > 0

    def followed_artists(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT mbid, name, followed_ts, last_check_ts, monitored,"
            " want_types,"
            " datetime(followed_ts,'unixepoch','localtime') AS followed"
            " FROM artists ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def artist_by_mbid(self, mbid: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT mbid, name, followed_ts, last_check_ts, monitored, want_types"
            " FROM artists WHERE mbid=?",
            (mbid,),
        ).fetchone()

    def reset_artist_check(self, mbid: str) -> bool:
        cur = self._conn.execute(
            "UPDATE artists SET last_check_ts=0 WHERE mbid=?", (mbid,)
        )
        self._commit()
        return cur.rowcount > 0

    def set_artist_option(
        self,
        mbid: str,
        monitored: bool | None = None,
        want_types: str | None = None,
    ) -> bool:
        sets: list[str] = []
        args: list[Any] = []
        if monitored is not None:
            sets.append("monitored=?")
            args.append(1 if monitored else 0)
        if want_types is not None:
            sets.append("want_types=?")
            args.append(want_types)
        if not sets:
            return False
        args.append(mbid)
        cur = self._conn.execute(
            f"UPDATE artists SET {', '.join(sets)} WHERE mbid=?", args
        )
        self._commit()
        return cur.rowcount > 0

    def artists_due_check(
        self, older_than_ts: int, limit: int
    ) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT mbid, name, followed_ts, want_types FROM artists"
            " WHERE last_check_ts < ? AND monitored=1"
            " ORDER BY last_check_ts LIMIT ?",
            (older_than_ts, limit),
        ).fetchall()

    # --- release calendar ---------------------------------------------------

    def upsert_calendar(
        self,
        rgid: str,
        artist_mbid: str,
        artist: str,
        album: str,
        date: str,
        rtype: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO calendar (rgid, artist_mbid, artist, album, date,"
            " rtype, updated_ts) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(rgid) DO UPDATE SET artist=excluded.artist,"
            " album=excluded.album, date=excluded.date, rtype=excluded.rtype,"
            " updated_ts=excluded.updated_ts",
            (rgid, artist_mbid, artist, album, date, rtype, int(time.time())),
        )
        self._commit()

    def calendar_rows(self, since: str, until: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT rgid, artist, album, date, rtype FROM calendar"
            " WHERE date >= ? AND date <= ? ORDER BY date",
            (since, until),
        ).fetchall()

    def mark_artist_checked(self, mbid: str) -> None:
        self._conn.execute(
            "UPDATE artists SET last_check_ts=? WHERE mbid=?",
            (int(time.time()), mbid),
        )
        self._commit()

    # --- playlist sync controls -------------------------------------------

    def upsert_playlist(
        self, spotify_id: str, name: str, track_count: int
    ) -> None:
        now = int(time.time())
        self._conn.execute(
            "INSERT INTO playlists (spotify_id, name, track_count, last_seen_ts)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(spotify_id) DO UPDATE SET"
            " name=excluded.name, track_count=excluded.track_count,"
            " last_seen_ts=excluded.last_seen_ts",
            (spotify_id, name, track_count, now),
        )
        self._commit()

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

    def set_source_status(
        self, source: str, from_status: str, to_status: str
    ) -> int:
        cur = self._conn.execute(
            "UPDATE wants SET status=?, updated_ts=? WHERE status=?"
            " AND source = ?",
            (to_status, int(time.time()), from_status, source),
        )
        self._commit()
        return cur.rowcount

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
        cur = self._conn.execute("DELETE FROM playlists WHERE gen != ?", (gen,))
        self._conn.execute(
            "DELETE FROM sp_playlist_tracks WHERE playlist_id NOT IN"
            " (SELECT spotify_id FROM playlists)"
        )
        self._prune_orphan_sp_tracks()
        self._commit()
        return cur.rowcount

    def sp_delete_playlist(self, spotify_id: str) -> str | None:
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
        self._commit()
        return name

    def sp_playlist_album_rows(self, playlist_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT DISTINCT t.album_id, t.artist, t.album, t.year,"
            " t.album_total FROM sp_playlist_tracks pt"
            " JOIN sp_tracks t ON t.track_id = pt.track_id"
            " WHERE pt.playlist_id=? AND t.album_id != ''",
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
    ) -> None:
        self._conn.execute(
            "INSERT INTO sp_tracks (track_id, title, artist, album, album_id,"
            " year, duration_s, album_total, liked_gen)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(track_id) DO UPDATE SET title=excluded.title,"
            " artist=excluded.artist, album=excluded.album,"
            " album_id=excluded.album_id, year=excluded.year,"
            " duration_s=excluded.duration_s,"
            " album_total=MAX(album_total, excluded.album_total)"
            + (", liked_gen=excluded.liked_gen" if liked_gen else ""),
            (
                track_id,
                title,
                artist,
                album,
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
        self._commit()

    def sp_playlist_track_rows(self, playlist_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT t.track_id, t.title, t.artist, t.album, t.album_id,"
            " t.year, t.duration_s, t.album_total, t.liked_gen"
            " FROM sp_playlist_tracks pt JOIN sp_tracks t"
            " ON t.track_id = pt.track_id"
            " WHERE pt.playlist_id=? ORDER BY pt.pos",
            (playlist_id,),
        ).fetchall()

    def sp_album_track_rows(self, album_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT track_id, title, artist, album, album_id, year,"
            " duration_s, album_total, liked_gen"
            " FROM sp_tracks WHERE album_id=? ORDER BY title",
            (album_id,),
        ).fetchall()

    def sp_cached_playlists(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT spotify_id, name, track_count FROM playlists"
            " WHERE tracks_state='ok' ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def sp_cached_playlist_ids(self) -> list[str]:
        return [r[0] for r in self.sp_cached_playlists()]

    def sp_unlinked_playlist_ids(self) -> list[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT spotify_id FROM playlists WHERE tracks_state != 'ok'"
                " ORDER BY hunt DESC, mirror DESC, name COLLATE NOCASE"
            )
        ]

    def sp_playlist_want_keys(self) -> set[str]:
        return {
            r[0]
            for r in self._conn.execute(
                "SELECT spotify_album_id FROM wants WHERE source LIKE 'playlist:%'"
            )
        }

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
        cur = self._conn.execute(
            "UPDATE sp_tracks SET liked_gen=0"
            " WHERE liked_gen > 0 AND liked_gen != ?",
            (gen,),
        )
        self._prune_orphan_sp_tracks()
        self._commit()
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
            "SELECT track_id, title, artist, album, album_id, year,"
            " duration_s, album_total"
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
        miss_retry_s: int = MISS_RETRY_SECONDS,
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

    def close(self) -> None:
        self._conn.close()


__all__ = ["HunterState", "MISS_RETRY_SECONDS", "Want"]

"""Library reads — the beets DB as a browsable catalogue (strictly mode=ro).

Layer 2 — uses ``hunter.db.open_readonly_db`` and ``hunter.domain``.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from hunter.config import HunterConfig
from hunter.db import open_readonly_db
from hunter.domain import (
    LOSSLESS_FORMATS,
    LOSSLESS_SQL,
    chunked,
    container_to_host_path,
    escape_like,
)

_LOSSLESS_EXPR = f"UPPER(COALESCE(i.format,'')) IN {LOSSLESS_SQL}"

_ALBUM_TOTAL_SUBQUERY = """
LEFT JOIN (
    SELECT album_id,
           CASE WHEN disctotal > 1 AND present_discs > 0 AND present_discs < disctotal THEN
               MAX(known_total,
                   (CASE WHEN known_total > 0 THEN known_total ELSE item_count END)
                   + (disctotal - present_discs)
                     * MAX(1, CAST(ROUND(1.0 * (CASE WHEN known_total > 0 THEN known_total ELSE item_count END) / present_discs) AS INTEGER)))
           ELSE known_total END AS expected_tracks
    FROM (
        SELECT album_id,
               SUM(disc_total) AS known_total,
               SUM(disc_items) AS item_count,
               COUNT(*) AS present_discs,
               MAX(max_dt) AS disctotal
        FROM (
            SELECT album_id,
                   COALESCE(NULLIF(disc, 0), 1) AS d,
                   COUNT(id) AS disc_items,
                   COALESCE(MAX(tracktotal), 0) AS disc_total,
                   COALESCE(MAX(disctotal), 0) AS max_dt
            FROM items
            WHERE album_id IS NOT NULL
            GROUP BY album_id, COALESCE(NULLIF(disc, 0), 1)
        )
        GROUP BY album_id
    )
) _at ON _at.album_id = a.id
"""

ALBUM_SORTS = {
    "artist": "a.albumartist COLLATE NOCASE, a.year, a.album COLLATE NOCASE",
    "album": "a.album COLLATE NOCASE",
    "year": "a.year DESC, a.albumartist COLLATE NOCASE",
    "year_asc": "a.year ASC, a.albumartist COLLATE NOCASE",
    "added": "a.added DESC",
    "tracks": "tracks DESC",
    "coverage": "coverage ASC, a.albumartist COLLATE NOCASE",
    "quality": "lossless_share ASC, a.albumartist COLLATE NOCASE",
    "genre": "a.genre COLLATE NOCASE, a.albumartist COLLATE NOCASE",
    "length": "length DESC",
    "bitrate": "max_bitrate DESC",
}

_ADDED_WINDOWS = {
    "1d": 86_400,
    "7d": 7 * 86_400,
    "30d": 30 * 86_400,
    "90d": 90 * 86_400,
    "365d": 365 * 86_400,
}


def connect(cfg: HunterConfig) -> sqlite3.Connection:
    return open_readonly_db(cfg.beets_db, timeout=15.0)


def host_path(cfg: HunterConfig, raw: str | bytes | None) -> Path | None:
    """Strictly confine container paths beneath ``cfg.music_root``."""
    return container_to_host_path(cfg, raw)


def text(raw: Any) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)


def _chunked_in(col: str, values: list[str], negate: bool = False) -> tuple[str, list[str]]:
    if not values:
        return ("1" if negate else "0"), []
    parts: list[str] = []
    args: list[str] = []
    op = "NOT IN" if negate else "IN"
    joiner = " AND " if negate else " OR "
    for batch in chunked(values, 400):
        ph = ",".join("?" * len(batch))
        parts.append(f"{col} {op} ({ph})")
        args.extend(batch)
    return "(" + joiner.join(parts) + ")", args


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------

def overview(cfg: HunterConfig) -> dict[str, Any]:
    conn = connect(cfg)
    try:
        now = time.time()
        c24, c7, c30 = now - 86_400, now - 604_800, now - 2_592_000

        items, lossless, hours, est_bytes, a24, a7, a30 = conn.execute(
            f"SELECT COUNT(*),"
            f" COALESCE(SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END), 0),"
            f" COALESCE(SUM(length), 0) / 3600.0,"
            f" COALESCE(SUM(length * bitrate / 8.0), 0),"
            f" COALESCE(SUM(CASE WHEN added > ? THEN 1 ELSE 0 END), 0),"
            f" COALESCE(SUM(CASE WHEN added > ? THEN 1 ELSE 0 END), 0),"
            f" COALESCE(SUM(CASE WHEN added > ? THEN 1 ELSE 0 END), 0)"
            f" FROM items i",
            (c24, c7, c30),
        ).fetchone()

        albums, artists, noart, no_mbid = conn.execute(
            "SELECT COUNT(*),"
            " COUNT(DISTINCT albumartist),"
            " COALESCE(SUM(CASE WHEN artpath IS NULL OR artpath = '' OR artpath = X'' THEN 1 ELSE 0 END), 0),"
            " COALESCE(SUM(CASE WHEN mb_albumid IS NULL OR mb_albumid = '' THEN 1 ELSE 0 END), 0)"
            " FROM albums"
        ).fetchone()

        incomplete, lossy_albums = conn.execute(
            f"SELECT"
            f" COALESCE(SUM(CASE WHEN cnt < expected_tracks THEN 1 ELSE 0 END), 0),"
            f" COALESCE(SUM(CASE WHEN ll = 0 THEN 1 ELSE 0 END), 0)"
            f" FROM ("
            f"   SELECT a.id,"
            f"          COUNT(i.id) AS cnt,"
            f"          COALESCE(MAX(_at.expected_tracks), 0) AS expected_tracks,"
            f"          SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) AS ll"
            f"   FROM albums a"
            f"   JOIN items i ON i.album_id = a.id"
            f"   {_ALBUM_TOTAL_SUBQUERY}"
            f"   GROUP BY a.id"
            f" )"
        ).fetchone()

        formats = [
            {"format": (r[0] or "?").upper(), "count": r[1]}
            for r in conn.execute(
                "SELECT format, COUNT(*) FROM items GROUP BY UPPER(format)"
                " ORDER BY COUNT(*) DESC LIMIT 12"
            )
        ]
        added = {"24h": a24, "7d": a7, "30d": a30}
        timeline = [
            {"day": r[0], "count": r[1]}
            for r in conn.execute(
                "SELECT date(added,'unixepoch','localtime') AS d, COUNT(*) FROM items"
                " WHERE added > ? GROUP BY d ORDER BY d",
                (c30,),
            )
        ]
        genres_list = [
            {"genre": r[0], "count": r[1]}
            for r in conn.execute(
                "SELECT genre, COUNT(*) FROM items WHERE genre != '' GROUP BY genre"
                " ORDER BY COUNT(*) DESC LIMIT 15"
            )
        ]
        decades = [
            {"decade": r[0], "count": r[1]}
            for r in conn.execute(
                "SELECT (year/10)*10 AS dec, COUNT(*) FROM albums WHERE year > 1900"
                " GROUP BY dec ORDER BY dec"
            )
        ]
        recent = [
            dict(r)
            for r in conn.execute(
                "SELECT a.id, a.albumartist AS artist, a.album, a.year,"
                " COUNT(i.id) AS tracks, MAX(i.added) AS added_ts"
                " FROM albums a JOIN items i ON i.album_id=a.id"
                " GROUP BY a.id ORDER BY added_ts DESC LIMIT 12"
            )
        ]
    finally:
        conn.close()

    return {
        "items": items,
        "albums": albums,
        "artists": artists,
        "lossless": lossless,
        "lossless_pct": round(100.0 * lossless / items, 1) if items else 0,
        "hours": round(hours, 1),
        "est_bytes": int(est_bytes),
        "formats": formats,
        "added": added,
        "timeline": timeline,
        "incomplete_albums": incomplete,
        "no_art_albums": noart,
        "lossy_albums": lossy_albums,
        "no_mbid_albums": no_mbid,
        "genres": genres_list,
        "decades": decades,
        "recent": recent,
    }


# --------------------------------------------------------------------------
# albums
# --------------------------------------------------------------------------

_ALBUM_SELECT = f"""
SELECT a.id, a.albumartist AS artist, a.album, a.year, a.genre, a.albumtype,
       a.mb_releasegroupid AS rgid, a.mb_albumid, a.comp, a.label, a.country,
       a.mb_albumartistid, a.added AS album_added,
       CASE WHEN a.artpath IS NULL OR a.artpath = '' OR a.artpath = X'' THEN 0 ELSE 1 END AS has_art,
       COUNT(i.id) AS tracks,
       COALESCE(MAX(_at.expected_tracks), 0) AS tracktotal,
       COALESCE(MAX(i.disctotal), 1) AS disctotal,
       SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) AS lossless,
       CAST(SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) AS REAL) / MAX(COUNT(i.id),1) AS lossless_share,
       CASE WHEN COALESCE(MAX(_at.expected_tracks),0) > 0
            THEN CAST(COUNT(i.id) AS REAL) / MAX(_at.expected_tracks) ELSE 1.0 END AS coverage,
       GROUP_CONCAT(DISTINCT UPPER(i.format)) AS formats,
       MAX(i.bitrate) AS max_bitrate, MAX(i.bitdepth) AS max_bitdepth,
       MAX(i.samplerate) AS max_samplerate,
       COALESCE(SUM(i.length),0) AS length,
       MAX(i.added) AS added_ts
FROM albums a
LEFT JOIN items i ON i.album_id = a.id
{_ALBUM_TOTAL_SUBQUERY}
"""


def _split_filters(filt: str) -> list[str]:
    return [f.strip() for f in (filt or "").split(",") if f.strip()]


def _album_clauses(
    *,
    q: str = "",
    artist: str = "",
    genre: str = "",
    label: str = "",
    year=None,
    decade=None,
    year_from=None,
    year_to=None,
    filt: str = "",
    fmt: str = "",
    albumtype: str = "",
    added: str = "",
    letter: str = "",
    min_tracks=None,
    max_tracks=None,
    names_in: list | None = None,
    names_not_in: list | None = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    having: list[str] = []
    args: list[Any] = []
    hargs: list[Any] = []

    if q:
        pat = f"%{escape_like(q)}%"
        where.append("(a.album LIKE ? ESCAPE '\\' OR a.albumartist LIKE ? ESCAPE '\\')")
        args.extend([pat, pat])
    if artist:
        where.append("a.albumartist = ?")
        args.append(artist)
    if genre:
        if genre == "__none__":
            where.append("(a.genre IS NULL OR a.genre = '')")
        else:
            where.append("a.genre = ?")
            args.append(genre)
    if label:
        if label == "__none__":
            where.append("(a.label IS NULL OR a.label = '')")
        else:
            where.append("a.label = ?")
            args.append(label)
    if year:
        where.append("a.year = ?")
        args.append(year)
    if decade:
        where.append("a.year >= ? AND a.year < ?")
        args.extend([decade, decade + 10])
    if year_from:
        where.append("a.year >= ?")
        args.append(year_from)
    if year_to:
        where.append("a.year <= ?")
        args.append(year_to)
    if albumtype:
        if albumtype == "__none__":
            where.append("(a.albumtype IS NULL OR a.albumtype = '')")
        else:
            where.append("a.albumtype = ?")
            args.append(albumtype)
    if letter:
        if letter == "#":
            where.append("NOT (UPPER(SUBSTR(a.albumartist,1,1)) BETWEEN 'A' AND 'Z')")
        else:
            where.append("UPPER(SUBSTR(a.albumartist,1,1)) = ?")
            args.append(letter.upper()[:1])
    if added in _ADDED_WINDOWS:
        where.append("a.added > ?")
        args.append(time.time() - _ADDED_WINDOWS[added])
    if names_in is not None:
        clause, cargs = _chunked_in("a.albumartist", list(names_in), negate=False)
        where.append(clause)
        args.extend(cargs)
    if names_not_in:
        clause, cargs = _chunked_in("a.albumartist", list(names_not_in), negate=True)
        where.append(clause)
        args.extend(cargs)

    for f in _split_filters(filt):
        if f == "incomplete":
            having.append("COUNT(i.id) < COALESCE(MAX(_at.expected_tracks),0)")
        elif f == "complete":
            having.append(
                "COUNT(i.id) > 0 AND COUNT(i.id) >= COALESCE(MAX(_at.expected_tracks),0)"
            )
        elif f == "noart":
            where.append("(a.artpath IS NULL OR a.artpath = '' OR a.artpath = X'')")
        elif f == "art":
            where.append("NOT (a.artpath IS NULL OR a.artpath = '' OR a.artpath = X'')")
        elif f == "lossy":
            having.append(
                f"SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) = 0"
                f" AND COUNT(i.id) > 0"
            )
        elif f == "lossless":
            having.append(
                f"SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) = COUNT(i.id)"
                f" AND COUNT(i.id) > 0"
            )
        elif f == "mixed":
            having.append(
                f"SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END)"
                f" BETWEEN 1 AND COUNT(i.id)-1"
            )
        elif f == "hires":
            having.append("(MAX(i.bitdepth) > 16 OR MAX(i.samplerate) > 48000)")
        elif f == "nombid":
            where.append("(a.mb_albumid IS NULL OR a.mb_albumid = '')")
        elif f == "mbid":
            where.append("(a.mb_albumid IS NOT NULL AND a.mb_albumid != '')")
        elif f in ("singles", "onetrack"):
            having.append("COUNT(i.id) = 1")
        elif f == "empty":
            having.append("COUNT(i.id) = 0")
        elif f == "comp":
            where.append("a.comp = 1")
        elif f == "nogenre":
            where.append("(a.genre IS NULL OR a.genre = '')")
        elif f == "noyear":
            where.append("(a.year IS NULL OR a.year = 0)")
        elif f == "recent":
            where.append("a.added > ?")
            args.append(time.time() - 7 * 86_400)
        elif f == "multidisc":
            having.append("COALESCE(MAX(i.disctotal),1) > 1")

    if fmt:
        having.append(
            "',' || COALESCE(GROUP_CONCAT(DISTINCT UPPER(i.format)), '') || ','"
            " LIKE ? ESCAPE '\\'"
        )
        hargs.append(f"%,{escape_like(fmt.upper())},%")
    if min_tracks is not None:
        having.append("COUNT(i.id) >= ?")
        hargs.append(int(min_tracks))
    if max_tracks is not None:
        having.append("COUNT(i.id) <= ?")
        hargs.append(int(max_tracks))

    sql = _ALBUM_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " GROUP BY a.id"
    if having:
        sql += " HAVING " + " AND ".join(having)
    return sql, args + hargs


def albums(
    cfg: HunterConfig,
    *,
    q: str = "",
    sort: str = "added",
    offset: int = 0,
    limit: int = 60,
    artist: str = "",
    genre: str = "",
    label: str = "",
    year: int | None = None,
    decade: int | None = None,
    filt: str = "",
    fmt: str = "",
    albumtype: str = "",
    added: str = "",
    letter: str = "",
    year_from: int | None = None,
    year_to: int | None = None,
    min_tracks: int | None = None,
    max_tracks: int | None = None,
) -> dict[str, Any]:
    sql, args = _album_clauses(
        q=q, artist=artist, genre=genre, label=label, year=year, decade=decade,
        year_from=year_from, year_to=year_to, filt=filt, fmt=fmt,
        albumtype=albumtype, added=added, letter=letter,
        min_tracks=min_tracks, max_tracks=max_tracks,
    )
    order = ALBUM_SORTS.get(sort, ALBUM_SORTS["added"])
    conn = connect(cfg)
    try:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = [
            dict(r)
            for r in conn.execute(
                f"{sql} ORDER BY {order} LIMIT ? OFFSET ?", args + [limit, offset]
            )
        ]
    finally:
        conn.close()
    for r in rows:
        r["coverage"] = round(r["coverage"] or 0, 3)
        r["lossless_share"] = round(r["lossless_share"] or 0, 3)
    return {"total": total, "albums": rows, "offset": offset, "limit": limit}


def album_ids(cfg: HunterConfig, cap: int = 5000, **kw) -> dict[str, Any]:
    sql, args = _album_clauses(**kw)
    conn = connect(cfg)
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, artist, album, tracks FROM ({sql}) LIMIT ?",
            args + [cap],
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "capped": total > cap,
        "albums": [
            {"id": r[0], "artist": r[1], "album": r[2], "tracks": r[3]}
            for r in rows
        ],
    }


_ALBUM_GROUP_KEYS = {
    "genre": "COALESCE(NULLIF(genre,''), '(no genre)')",
    "decade": "CASE WHEN year > 0 THEN (year/10)*10 ELSE 0 END",
    "year": "COALESCE(year, 0)",
    "type": "COALESCE(NULLIF(albumtype,''), '(untyped)')",
    "artist": "artist",
    "letter": (
        "CASE WHEN UPPER(SUBSTR(artist,1,1)) BETWEEN 'A' AND 'Z'"
        " THEN UPPER(SUBSTR(artist,1,1)) ELSE '#' END"
    ),
    "quality": (
        "CASE WHEN tracks = 0 THEN 'empty'"
        " WHEN lossless_share >= 1 THEN 'lossless'"
        " WHEN lossless_share > 0 THEN 'mixed' ELSE 'lossy' END"
    ),
    "format": "COALESCE(formats, '')",
    "label": "COALESCE(NULLIF(label,''), '(no label)')",
    "country": "COALESCE(NULLIF(country,''), '(unknown)')",
}


def album_groups(
    cfg: HunterConfig, by: str = "genre", limit: int = 400, **kw
) -> dict[str, Any]:
    key = _ALBUM_GROUP_KEYS.get(by)
    if not key:
        return {"by": by, "groups": [], "error": "unknown group key"}
    sql, args = _album_clauses(**kw)
    conn = connect(cfg)
    try:
        rows = conn.execute(
            f"SELECT {key} AS k, COUNT(*) AS albums, SUM(tracks) AS tracks,"
            f" COUNT(DISTINCT artist) AS artists,"
            f" ROUND(AVG(lossless_share),3), SUM(length) FROM ({sql})"
            f" GROUP BY k ORDER BY albums DESC, k LIMIT ?",
            args + [limit],
        ).fetchall()
    finally:
        conn.close()
    if by == "format":
        # Merge format-combo permutations ("FLAC,MP3" == "MP3,FLAC") deterministically.
        merged: dict[str, dict[str, Any]] = {}
        for r in rows:
            canon = ",".join(sorted(f.strip() for f in str(r[0]).split(",") if f.strip()))
            cur = merged.get(canon)
            if cur is None:
                merged[canon] = {
                    "key": canon,
                    "albums": r[1],
                    "tracks": r[2] or 0,
                    "artists": r[3],
                    "_ll_weighted": (r[4] or 0) * r[1],
                    "_secs": r[5] or 0,
                }
            else:
                cur["albums"] += r[1]
                cur["tracks"] += r[2] or 0
                cur["artists"] += r[3]
                cur["_ll_weighted"] += (r[4] or 0) * r[1]
                cur["_secs"] += r[5] or 0
        ordered = sorted(merged.values(), key=lambda x: (-x["albums"], x["key"]))[:limit]
        return {
            "by": by,
            "groups": [
                {
                    "key": g["key"],
                    "albums": g["albums"],
                    "tracks": g["tracks"],
                    "artists": g["artists"],
                    "lossless_share": round(g["_ll_weighted"] / max(g["albums"], 1), 3),
                    "hours": round(g["_secs"] / 3600, 1),
                }
                for g in ordered
            ],
        }
    return {
        "by": by,
        "groups": [
            {
                "key": str(r[0]),
                "albums": r[1],
                "tracks": r[2] or 0,
                "artists": r[3],
                "lossless_share": r[4] or 0,
                "hours": round((r[5] or 0) / 3600, 1),
            }
            for r in rows
        ],
    }


def album(cfg: HunterConfig, album_id: int) -> dict[str, Any] | None:
    conn = connect(cfg)
    try:
        conn.execute("BEGIN")
        head = conn.execute(
            _ALBUM_SELECT.replace(
                "a.mb_albumartistid, a.added AS album_added,",
                "a.mb_albumartistid, a.added AS album_added, a.artpath,"
                " a.albumartist_sort, a.albumstatus, a.albumdisambig,"
                " a.catalognum, a.original_year,",
            )
            + " WHERE a.id = ? GROUP BY a.id",
            (album_id,),
        ).fetchone()
        if not head:
            return None
        out = dict(head)
        items = [
            dict(r)
            for r in conn.execute(
                "SELECT id, disc, track, title, artist, length, bitrate, format,"
                " samplerate, bitdepth, channels, genre, bpm, initial_key,"
                " mb_trackid, mb_albumid, acoustid_id, path, added, comments,"
                " lyrics != '' AS has_lyrics, mtime, tracktotal"
                " FROM items WHERE album_id=?"
                " ORDER BY disc, track, title COLLATE NOCASE",
                (album_id,),
            )
        ]
    finally:
        conn.close()

    raw_art = out.get("artpath")
    art = host_path(cfg, raw_art)
    out["artpath"] = text(raw_art)
    out["art_exists"] = bool(art and art.is_file())

    dirs: set[str] = set()
    missing_files = 0
    disc_totals: dict[int, int] = {}
    have: set[tuple[int, int]] = set()
    have_tracks_any_disc: set[int] = set()

    for it in items:
        raw_path = it["path"]
        hp = host_path(cfg, raw_path)
        it["exists"] = bool(hp and hp.is_file())
        if not it["exists"]:
            missing_files += 1
        p = text(raw_path)
        it["path"] = p
        it["file"] = os.path.basename(p)
        dirs.add(os.path.dirname(p))
        it["lossless"] = (it["format"] or "").upper() in LOSSLESS_FORMATS
        it["kbps"] = int((it["bitrate"] or 0) / 1000)

        disc_no = it["disc"] or 1
        if it["track"]:
            have.add((disc_no, it["track"]))
            have_tracks_any_disc.add(it["track"])
        tt = it.get("tracktotal") or 0
        if tt > disc_totals.get(disc_no, 0):
            disc_totals[disc_no] = tt

    out["items"] = items
    out["dirs"] = sorted(dirs)
    out["missing_files"] = missing_files

    disctotal = min(int(out.get("disctotal") or 1), 100)
    missing_tracks: list[str] = []
    if disctotal <= 1:
        total = min(int(out.get("tracktotal") or 0), 500)
        if total:
            for n in range(1, total + 1):
                if n not in have_tracks_any_disc:
                    missing_tracks.append(str(n))
                    if len(missing_tracks) >= 60:
                        break
    else:
        all_discs = sorted(set(disc_totals) | set(range(1, disctotal + 1)))
        for d in all_discs:
            dt = min(int(disc_totals.get(d, 0)), 500)
            if dt <= 0:
                missing_tracks.append(f"disc {d}")
                if len(missing_tracks) >= 60:
                    break
                continue
            for n in range(1, dt + 1):
                if (d, n) not in have:
                    missing_tracks.append(f"{d}.{n}")
                    if len(missing_tracks) >= 60:
                        break
            if len(missing_tracks) >= 60:
                break
    out["missing_tracks"] = missing_tracks[:60]
    return out


def album_art_path(cfg: HunterConfig, album_id: int) -> Path | None:
    conn = connect(cfg)
    try:
        r = conn.execute(
            "SELECT artpath FROM albums WHERE id=?", (album_id,)
        ).fetchone()
    finally:
        conn.close()
    if not r or not r["artpath"]:
        return None
    p = host_path(cfg, r["artpath"])
    return p if p and p.is_file() else None


# --------------------------------------------------------------------------
# artists
# --------------------------------------------------------------------------

_ARTIST_SELECT = f"""
SELECT a.albumartist AS name, COUNT(DISTINCT a.id) AS albums,
       COUNT(i.id) AS tracks, MAX(i.added) AS last_added,
       CAST(SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) AS REAL)/MAX(COUNT(i.id),1) AS lossless_share,
       MIN(NULLIF(a.year,0)) AS first_year, MAX(a.year) AS last_year,
       COALESCE(MAX(NULLIF(a.mb_albumartistid, '')), '') AS mbid,
       MIN(tg.genre) AS genre,
       COUNT(DISTINCT CASE WHEN a.mb_albumid IS NULL OR a.mb_albumid = '' THEN a.id END) AS albums_nombid,
       COALESCE(SUM(i.length),0) AS length
FROM albums a LEFT JOIN items i ON i.album_id = a.id
LEFT JOIN top_genre tg ON tg.albumartist = a.albumartist
"""

_TOP_GENRE_CTE = """WITH top_genre AS (
  SELECT albumartist, genre FROM (
    SELECT albumartist, genre, ROW_NUMBER() OVER (PARTITION BY albumartist ORDER BY COUNT(*) DESC, genre) AS rn
    FROM albums WHERE genre != '' GROUP BY albumartist, genre)
  WHERE rn = 1)
"""

_ARTIST_SORTS = {
    "name": "name COLLATE NOCASE",
    "albums": "albums DESC, name COLLATE NOCASE",
    "tracks": "tracks DESC",
    "added": "last_added DESC",
    "quality": "lossless_share ASC, name COLLATE NOCASE",
    "year": "first_year ASC, name COLLATE NOCASE",
    "newest": "last_year DESC, name COLLATE NOCASE",
    "genre": "genre COLLATE NOCASE, name COLLATE NOCASE",
    "length": "length DESC",
}


def _artist_clauses(
    *,
    q: str = "",
    letter: str = "",
    genre: str = "",
    decade=None,
    filt: str = "",
    added: str = "",
    min_albums=None,
    max_albums=None,
    names_in: list | None = None,
    names_not_in: list | None = None,
) -> tuple[str, list[Any]]:
    where: list[str] = ["a.albumartist != ''"]
    having: list[str] = []
    args: list[Any] = []
    hargs: list[Any] = []

    if q:
        where.append("a.albumartist LIKE ? ESCAPE '\\'")
        args.append(f"%{escape_like(q)}%")
    if letter:
        if letter == "#":
            where.append("NOT (UPPER(SUBSTR(a.albumartist,1,1)) BETWEEN 'A' AND 'Z')")
        else:
            where.append("UPPER(SUBSTR(a.albumartist,1,1)) = ?")
            args.append(letter.upper()[:1])
    if genre:
        if genre == "__none__":
            having.append("MIN(tg.genre) IS NULL")
        else:
            where.append("a.albumartist IN (SELECT albumartist FROM albums WHERE genre = ?)")
            args.append(genre)
    if decade:
        where.append("a.albumartist IN (SELECT albumartist FROM albums WHERE year >= ? AND year < ?)")
        args.extend([decade, decade + 10])
    if added in _ADDED_WINDOWS:
        having.append("MAX(i.added) > ?")
        hargs.append(time.time() - _ADDED_WINDOWS[added])
    if names_in is not None:
        clause, cargs = _chunked_in("a.albumartist", list(names_in), negate=False)
        where.append(clause)
        args.extend(cargs)
    if names_not_in:
        clause, cargs = _chunked_in("a.albumartist", list(names_not_in), negate=True)
        where.append(clause)
        args.extend(cargs)

    for f in _split_filters(filt):
        if f == "lossy":
            having.append(
                f"SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) = 0"
                f" AND COUNT(i.id) > 0"
            )
        elif f == "lossless":
            having.append(
                f"SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) = COUNT(i.id)"
                f" AND COUNT(i.id) > 0"
            )
        elif f == "mixed":
            having.append(
                f"SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END)"
                f" BETWEEN 1 AND COUNT(i.id)-1"
            )
        elif f == "one_album":
            having.append("COUNT(DISTINCT a.id) = 1")
        elif f == "one_track":
            having.append("COUNT(i.id) = 1")
        elif f == "nombid":
            having.append(
                "COUNT(DISTINCT CASE WHEN a.mb_albumid IS NULL OR a.mb_albumid = ''"
                " THEN a.id END) > 0"
            )
        elif f == "noartistid":
            where.append(
                "a.albumartist IN (SELECT DISTINCT albumartist FROM albums"
                " WHERE mb_albumartistid IS NULL OR mb_albumartistid = '')"
            )
        elif f == "nogenre":
            having.append("MIN(tg.genre) IS NULL")
        elif f == "incomplete":
            where.append(
                f"a.albumartist IN ("
                f"  SELECT DISTINCT a2.albumartist FROM albums a2"
                f"  LEFT JOIN items i2 ON i2.album_id = a2.id"
                f"  {_ALBUM_TOTAL_SUBQUERY.replace('a.id', 'a2.id')}"
                f"  GROUP BY a2.id"
                f"  HAVING COUNT(i2.id) < COALESCE(MAX(_at.expected_tracks), 0)"
                f")"
            )

    if min_albums is not None:
        having.append("COUNT(DISTINCT a.id) >= ?")
        hargs.append(int(min_albums))
    if max_albums is not None:
        having.append("COUNT(DISTINCT a.id) <= ?")
        hargs.append(int(max_albums))

    sql = (
        _TOP_GENRE_CTE
        + _ARTIST_SELECT
        + " WHERE "
        + " AND ".join(where)
        + " GROUP BY a.albumartist"
    )
    if having:
        sql += " HAVING " + " AND ".join(having)
    return sql, args + hargs


def artists(
    cfg: HunterConfig,
    *,
    q: str = "",
    offset: int = 0,
    limit: int = 100,
    sort: str = "name",
    letter: str = "",
    genre: str = "",
    decade: int | None = None,
    filt: str = "",
    added: str = "",
    min_albums: int | None = None,
    max_albums: int | None = None,
    names_in: list | None = None,
    names_not_in: list | None = None,
) -> dict[str, Any]:
    sql, args = _artist_clauses(
        q=q, letter=letter, genre=genre, decade=decade, filt=filt,
        added=added, min_albums=min_albums, max_albums=max_albums,
        names_in=names_in, names_not_in=names_not_in,
    )
    conn = connect(cfg)
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = [
            dict(r)
            for r in conn.execute(
                f"{sql} ORDER BY {_ARTIST_SORTS.get(sort, _ARTIST_SORTS['name'])}"
                f" LIMIT ? OFFSET ?",
                args + [limit, offset],
            )
        ]
    finally:
        conn.close()
    for r in rows:
        r["lossless_share"] = round(r["lossless_share"] or 0, 3)
        r["hours"] = round((r.pop("length", 0) or 0) / 3600, 1)
    return {"total": total, "artists": rows}


def artist_names(cfg: HunterConfig, cap: int = 5000, **kw) -> dict[str, Any]:
    sql, args = _artist_clauses(**kw)
    conn = connect(cfg)
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT name, albums, tracks FROM ({sql}) ORDER BY name COLLATE NOCASE LIMIT ?",
            args + [cap],
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "capped": total > cap,
        "artists": [
            {"name": r[0], "albums": r[1], "tracks": r[2]} for r in rows
        ],
    }


_ARTIST_GROUP_KEYS = {
    "genre": "COALESCE(genre, '(no genre)')",
    "decade": "CASE WHEN first_year > 0 THEN (first_year/10)*10 ELSE 0 END",
    "quality": (
        "CASE WHEN lossless_share >= 1 THEN 'lossless'"
        " WHEN lossless_share > 0 THEN 'mixed' ELSE 'lossy' END"
    ),
    "letter": (
        "CASE WHEN UPPER(SUBSTR(name,1,1)) BETWEEN 'A' AND 'Z'"
        " THEN UPPER(SUBSTR(name,1,1)) ELSE '#' END"
    ),
    "size": (
        "CASE WHEN albums >= 10 THEN '10+ albums'"
        " WHEN albums >= 4 THEN '4-9 albums'"
        " WHEN albums >= 2 THEN '2-3 albums' ELSE '1 album' END"
    ),
}


def artist_groups(
    cfg: HunterConfig, by: str = "genre", limit: int = 400, **kw
) -> dict[str, Any]:
    key = _ARTIST_GROUP_KEYS.get(by)
    if not key:
        return {"by": by, "groups": [], "error": "unknown group key"}
    sql, args = _artist_clauses(**kw)
    conn = connect(cfg)
    try:
        rows = conn.execute(
            f"SELECT {key} AS k, COUNT(*) AS artists, SUM(albums) AS albums,"
            f" SUM(tracks) AS tracks, ROUND(AVG(lossless_share),3), SUM(length)"
            f" FROM ({sql}) GROUP BY k ORDER BY artists DESC, k LIMIT ?",
            args + [limit],
        ).fetchall()
    finally:
        conn.close()
    return {
        "by": by,
        "groups": [
            {
                "key": str(r[0]),
                "artists": r[1],
                "albums": r[2] or 0,
                "tracks": r[3] or 0,
                "lossless_share": r[4] or 0,
                "hours": round((r[5] or 0) / 3600, 1),
            }
            for r in rows
        ],
    }


def artist(cfg: HunterConfig, name: str) -> dict[str, Any] | None:
    conn = connect(cfg)
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                _ALBUM_SELECT + " WHERE a.albumartist = ? GROUP BY a.id"
                " ORDER BY a.year, a.album COLLATE NOCASE",
                (name,),
            )
        ]
        if not rows:
            return None
        genres_list = [
            r[0]
            for r in conn.execute(
                "SELECT genre FROM items WHERE albumartist=? AND genre != ''"
                " GROUP BY genre ORDER BY COUNT(*) DESC LIMIT 6",
                (name,),
            )
        ]
        tracks_cnt, hours = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(length),0)/3600.0 FROM items WHERE albumartist=?",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    for r in rows:
        r["coverage"] = round(r["coverage"] or 0, 3)
        r["lossless_share"] = round(r["lossless_share"] or 0, 3)
    return {
        "name": name,
        "albums": rows,
        "genres": genres_list,
        "tracks": tracks_cnt,
        "hours": round(hours, 1),
        "mbid": next(
            (r["mb_albumartistid"] for r in rows if r.get("mb_albumartistid")),
            "",
        ),
    }


# --------------------------------------------------------------------------
# tracks
# --------------------------------------------------------------------------

_TRACK_SORTS = {
    "artist": "i.artist COLLATE NOCASE, a.album, i.disc, i.track",
    "album": "a.album COLLATE NOCASE, i.disc, i.track",
    "title": "i.title COLLATE NOCASE",
    "added": "i.added DESC",
    "year": "a.year DESC, a.album, i.track",
    "bitrate": "i.bitrate DESC",
    "bitrate_asc": "i.bitrate ASC",
    "length": "i.length DESC",
    "length_asc": "i.length ASC",
    "bpm": "i.bpm DESC",
    "genre": "i.genre COLLATE NOCASE, i.artist COLLATE NOCASE",
}
_TRACK_FROM = " FROM items i LEFT JOIN albums a ON i.album_id=a.id"


def _track_clauses(
    *,
    q: str = "",
    fmt: str = "",
    genre: str = "",
    album_id=None,
    artist: str = "",
    year=None,
    decade=None,
    filt: str = "",
    added: str = "",
    min_kbps=None,
    max_kbps=None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if q:
        pat = f"%{escape_like(q)}%"
        where.append(
            "(i.artist LIKE ? ESCAPE '\\' OR i.title LIKE ? ESCAPE '\\'"
            " OR a.album LIKE ? ESCAPE '\\')"
        )
        args.extend([pat, pat, pat])
    if genre:
        if genre == "__none__":
            where.append("(i.genre IS NULL OR i.genre = '')")
        else:
            where.append("i.genre = ?")
            args.append(genre)
    if album_id:
        where.append("i.album_id = ?")
        args.append(album_id)
    if artist:
        where.append("(i.albumartist = ? OR i.artist = ?)")
        args.extend([artist, artist])
    if year:
        where.append("a.year = ?")
        args.append(year)
    if decade:
        where.append("a.year >= ? AND a.year < ?")
        args.extend([decade, decade + 10])
    if added in _ADDED_WINDOWS:
        where.append("i.added > ?")
        args.append(time.time() - _ADDED_WINDOWS[added])
    fmt = (fmt or "").upper()
    if fmt == "LOSSLESS":
        where.append(_LOSSLESS_EXPR)
    elif fmt == "LOSSY":
        where.append(f"NOT {_LOSSLESS_EXPR}")
    elif fmt:
        where.append("UPPER(i.format) = ?")
        args.append(fmt)
    if min_kbps is not None:
        where.append("i.bitrate >= ?")
        args.append(int(min_kbps) * 1000)
    if max_kbps is not None:
        where.append("i.bitrate <= ?")
        args.append(int(max_kbps) * 1000)
    for f in _split_filters(filt):
        if f == "short":
            where.append("i.length > 0 AND i.length < 45")
        elif f == "long":
            where.append("i.length > 1200")
        elif f == "lowbit":
            where.append(f"NOT {_LOSSLESS_EXPR} AND i.bitrate > 0 AND i.bitrate < 128000")
        elif f == "nombid":
            where.append("(i.mb_trackid IS NULL OR i.mb_trackid = '')")
        elif f == "untitled":
            where.append(
                "(i.title = '' OR i.artist = ''"
                " OR LOWER(i.title) IN ('unknown','untitled','track')"
                " OR LOWER(i.artist) IN ('unknown','unknown artist','various artists','va'))"
            )
        elif f == "nogenre":
            where.append("(i.genre IS NULL OR i.genre = '')")
        elif f == "noalbum":
            where.append("(i.album_id IS NULL OR a.id IS NULL)")
        elif f == "lone_va":
            where.append(
                "a.comp = 1 AND a.id IN"
                " (SELECT album_id FROM items GROUP BY album_id HAVING COUNT(*) = 1)"
            )
        elif f == "lone":
            where.append(
                "a.id IN (SELECT album_id FROM items GROUP BY album_id HAVING COUNT(*) = 1)"
            )
        elif f == "hires":
            where.append("(i.bitdepth > 16 OR i.samplerate > 48000)")
        elif f == "nofingerprint":
            where.append("(i.acoustid_id IS NULL OR i.acoustid_id = '')")
        elif f == "nobpm":
            where.append("(i.bpm IS NULL OR i.bpm = 0)")
        elif f == "nolyrics":
            where.append("(i.lyrics IS NULL OR i.lyrics = '')")
        elif f == "notrack":
            where.append("(i.track IS NULL OR i.track = 0)")
    return (" WHERE " + " AND ".join(where)) if where else "", args


def tracks(
    cfg: HunterConfig,
    *,
    q: str = "",
    fmt: str = "",
    sort: str = "added",
    offset: int = 0,
    limit: int = 100,
    genre: str = "",
    album_id: int | None = None,
    artist: str = "",
    year: int | None = None,
    decade: int | None = None,
    filt: str = "",
    added: str = "",
    min_kbps: int | None = None,
    max_kbps: int | None = None,
) -> dict[str, Any]:
    wsql, args = _track_clauses(
        q=q, fmt=fmt, genre=genre, album_id=album_id, artist=artist,
        year=year, decade=decade, filt=filt, added=added,
        min_kbps=min_kbps, max_kbps=max_kbps,
    )
    conn = connect(cfg)
    try:
        conn.execute("BEGIN")
        total = conn.execute(
            "SELECT COUNT(*)" + _TRACK_FROM + wsql, args
        ).fetchone()[0]
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT i.id, i.artist, i.albumartist, i.title, a.album,"
                " i.album_id, a.year, i.genre, i.format, i.bitrate/1000 AS kbps,"
                " i.bitdepth, i.samplerate, CAST(i.length AS INT) AS secs,"
                " i.bpm, i.track, i.disc, a.mb_releasegroupid AS rgid,"
                " i.added, i.mb_trackid, a.comp,"
                f" CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END AS lossless"
                + _TRACK_FROM
                + wsql
                + f" ORDER BY {_TRACK_SORTS.get(sort, _TRACK_SORTS['added'])}"
                " LIMIT ? OFFSET ?",
                args + [limit, offset],
            )
        ]
    finally:
        conn.close()
    return {"total": total, "tracks": rows}


def track_ids(cfg: HunterConfig, cap: int = 5000, **kw) -> dict[str, Any]:
    wsql, args = _track_clauses(**kw)
    conn = connect(cfg)
    try:
        total = conn.execute(
            "SELECT COUNT(*)" + _TRACK_FROM + wsql, args
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT i.id, i.artist, i.title" + _TRACK_FROM + wsql + " LIMIT ?",
            args + [cap],
        ).fetchall()
    finally:
        conn.close()
    return {
        "total": total,
        "capped": total > cap,
        "tracks": [
            {"id": r[0], "artist": r[1], "title": r[2]} for r in rows
        ],
    }


_TRACK_GROUP_KEYS = {
    "genre": "COALESCE(NULLIF(i.genre,''), '(no genre)')",
    "format": "UPPER(COALESCE(i.format,'?'))",
    "decade": "CASE WHEN a.year > 0 THEN (a.year/10)*10 ELSE 0 END",
    "year": "COALESCE(a.year, 0)",
    "kbps": (
        "CASE WHEN " + _LOSSLESS_EXPR + " THEN 'lossless'"
        " WHEN i.bitrate >= 320000 THEN '320k'"
        " WHEN i.bitrate >= 256000 THEN '256k'"
        " WHEN i.bitrate >= 192000 THEN '192k'"
        " WHEN i.bitrate >= 128000 THEN '128k' ELSE '<128k' END"
    ),
    "artist": "i.albumartist",
    "album": "a.album",
}


def track_groups(
    cfg: HunterConfig, by: str = "genre", limit: int = 400, **kw
) -> dict[str, Any]:
    key = _TRACK_GROUP_KEYS.get(by)
    if not key:
        return {"by": by, "groups": [], "error": "unknown group key"}
    wsql, args = _track_clauses(**kw)
    conn = connect(cfg)
    try:
        rows = conn.execute(
            f"SELECT {key} AS k, COUNT(*) AS tracks,"
            f" COUNT(DISTINCT i.album_id) AS albums,"
            f" COUNT(DISTINCT i.albumartist) AS artists, SUM(i.length) AS length,"
            f" ROUND(AVG(CASE WHEN {_LOSSLESS_EXPR} THEN 1.0 ELSE 0.0 END),3)"
            + _TRACK_FROM
            + wsql
            + " GROUP BY k ORDER BY tracks DESC, k LIMIT ?",
            args + [limit],
        ).fetchall()
    finally:
        conn.close()
    return {
        "by": by,
        "groups": [
            {
                "key": str(r[0]),
                "tracks": r[1],
                "albums": r[2],
                "artists": r[3],
                "hours": round((r[4] or 0) / 3600, 1),
                "lossless_share": r[5] or 0,
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------
# duplicates + junk
# --------------------------------------------------------------------------

_DUP_KEY = (
    "CASE WHEN i.mb_trackid IS NOT NULL AND i.mb_trackid != ''"
    " THEN 'mb:' || i.mb_trackid"
    " ELSE 'tag:' || LOWER(TRIM(i.artist)) || '|' || LOWER(TRIM(i.title))"
    " || '|' || CAST(ROUND(i.length/4.0) AS INT) END"
)


def duplicates(
    cfg: HunterConfig,
    *,
    q: str = "",
    scope: str = "all",
    offset: int = 0,
    limit: int = 60,
    sort: str = "waste",
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []
    if q:
        pat = f"%{escape_like(q)}%"
        where.append(
            "(i.artist LIKE ? ESCAPE '\\' OR i.title LIKE ? ESCAPE '\\'"
            " OR a.album LIKE ? ESCAPE '\\')"
        )
        args.extend([pat, pat, pat])
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    hav = {
        "cross_album": " AND COUNT(DISTINCT i.album_id) > 1",
        "same_album": " AND COUNT(DISTINCT i.album_id) = 1",
    }.get(scope, "")
    order = {
        "waste": "waste DESC",
        "count": "n DESC, artist",
        "artist": "artist COLLATE NOCASE, title",
    }.get(sort, "waste DESC")
    grp = (
        f"SELECT {_DUP_KEY} AS k, COUNT(*) AS n, MIN(i.artist) AS artist,"
        f" MIN(i.title) AS title,"
        f" SUM(i.length * COALESCE(i.bitrate,0) / 8)"
        f" - MAX(i.length * COALESCE(i.bitrate,0) / 8) AS waste,"
        f" COUNT(DISTINCT i.album_id) AS albums"
        + _TRACK_FROM
        + wsql
        + f" GROUP BY k HAVING COUNT(*) > 1{hav}"
    )
    conn = connect(cfg)
    try:
        conn.execute("BEGIN")
        total, extra_tracks = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(n - 1),0) FROM ({grp})", args
        ).fetchone()
        keys = conn.execute(
            f"SELECT k, n, waste FROM ({grp}) ORDER BY {order} LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
        by_key: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in keys}
        if keys:
            key_list = [k for k, _, _ in keys]
            k_clause, k_args = _chunked_in(_DUP_KEY, key_list, negate=False)
            extra_where = f" AND ({' AND '.join(where)})" if where else ""
            for r in conn.execute(
                f"SELECT {_DUP_KEY} AS k, i.id, i.artist, i.albumartist,"
                f" i.title, a.album, i.album_id, a.year, a.albumtype, a.comp,"
                f" i.format, i.bitrate/1000 AS kbps, i.bitdepth, i.samplerate,"
                f" CAST(i.length AS INT) AS secs, i.track, i.disc, i.added,"
                f" i.path, i.mb_trackid,"
                f" CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END AS lossless"
                + _TRACK_FROM
                + f" WHERE {k_clause}{extra_where}",
                k_args + args,
            ):
                d = dict(r)
                d["path"] = text(d["path"])
                by_key[d.pop("k")].append(d)
        groups = []
        for k, n, waste in keys:
            rows = sorted(
                by_key.get(k, []),
                key=lambda r: (
                    -r["lossless"],
                    -(r["bitdepth"] or 0),
                    -(r["samplerate"] or 0),
                    -(r["kbps"] or 0),
                    r["added"] or 0,
                ),
            )
            groups.append(
                {
                    "key": k,
                    "count": len(rows),
                    "waste_bytes": int(waste or 0),
                    "by": "mbid" if k.startswith("mb:") else "tags",
                    "suggest_keep": rows[0]["id"] if rows else None,
                    "tracks": rows,
                }
            )
    finally:
        conn.close()
    return {
        "total": total,
        "extra_tracks": int(extra_tracks or 0),
        "groups": groups,
        "offset": offset,
        "limit": limit,
    }


def junk(cfg: HunterConfig) -> dict[str, Any]:
    """Counts for every junk signal, aligned with ``_album_clauses``."""
    conn = connect(cfg)
    try:
        t_row = conn.execute(
            f"WITH album_counts AS ("
            f"  SELECT album_id, COUNT(*) AS cnt FROM items"
            f"  WHERE album_id IS NOT NULL GROUP BY album_id"
            f")"
            f" SELECT"
            f"  COALESCE(SUM(CASE WHEN i.length > 0 AND i.length < 45 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN NOT {_LOSSLESS_EXPR} AND i.bitrate > 0 AND i.bitrate < 128000 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN i.title = '' OR i.artist = ''"
            f"    OR LOWER(i.title) IN ('unknown','untitled','track')"
            f"    OR LOWER(i.artist) IN ('unknown','unknown artist','various artists','va') THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN i.mb_trackid IS NULL OR i.mb_trackid = '' THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN a.comp = 1 AND ac.cnt = 1 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN a.id IS NULL THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN i.acoustid_id IS NULL OR i.acoustid_id = '' THEN 1 ELSE 0 END), 0)"
            f" FROM items i"
            f" LEFT JOIN albums a ON i.album_id = a.id"
            f" LEFT JOIN album_counts ac ON ac.album_id = a.id"
        ).fetchone()
        t = {
            "short": t_row[0],
            "lowbit": t_row[1],
            "untitled": t_row[2],
            "nombid": t_row[3],
            "lone_va": t_row[4],
            "noalbum": t_row[5],
            "nofingerprint": t_row[6],
        }

        al_row = conn.execute(
            f"SELECT"
            f"  COALESCE(SUM(CASE WHEN tracks = 1 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN tracks = 0 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN nombid = 1 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN noart = 1 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN tracks < expected_tracks THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN noyear = 1 THEN 1 ELSE 0 END), 0),"
            f"  COALESCE(SUM(CASE WHEN nogenre = 1 THEN 1 ELSE 0 END), 0)"
            f" FROM ("
            f"  SELECT a.id,"
            f"         COUNT(i.id) AS tracks,"
            f"         COALESCE(MAX(_at.expected_tracks), 0) AS expected_tracks,"
            f"         CASE WHEN a.mb_albumid IS NULL OR a.mb_albumid = '' THEN 1 ELSE 0 END AS nombid,"
            f"         CASE WHEN a.artpath IS NULL OR a.artpath = '' OR a.artpath = X'' THEN 1 ELSE 0 END AS noart,"
            f"         CASE WHEN a.year IS NULL OR a.year = 0 THEN 1 ELSE 0 END AS noyear,"
            f"         CASE WHEN a.genre IS NULL OR a.genre = '' THEN 1 ELSE 0 END AS nogenre"
            f"  FROM albums a"
            f"  LEFT JOIN items i ON i.album_id = a.id"
            f"  {_ALBUM_TOTAL_SUBQUERY}"
            f"  GROUP BY a.id"
            f" )"
        ).fetchone()
        al = {
            "onetrack": al_row[0],
            "empty": al_row[1],
            "nombid": al_row[2],
            "noart": al_row[3],
            "incomplete": al_row[4],
            "noyear": al_row[5],
            "nogenre": al_row[6],
        }

        dup_groups, dup_extra = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(n-1),0) FROM"
            f" (SELECT {_DUP_KEY} AS k, COUNT(*) AS n{_TRACK_FROM}"
            f" GROUP BY k HAVING COUNT(*) > 1)"
        ).fetchone()

        one_track_artists = conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT a.albumartist FROM albums a"
            "  JOIN items i ON i.album_id = a.id"
            "  WHERE a.albumartist != ''"
            "  GROUP BY a.albumartist HAVING COUNT(i.id) = 1"
            ")"
        ).fetchone()[0]
        no_artist_id = conn.execute(
            "SELECT COUNT(DISTINCT albumartist) FROM albums"
            " WHERE albumartist != ''"
            " AND (mb_albumartistid IS NULL OR mb_albumartistid = '')"
        ).fetchone()[0]
        ar = {"one_track": one_track_artists, "noartistid": no_artist_id}
    finally:
        conn.close()

    return {
        "tracks": t,
        "albums": al,
        "artists": ar,
        "duplicates": {"groups": dup_groups, "extra_tracks": int(dup_extra or 0)},
    }


def track(cfg: HunterConfig, item_id: int) -> dict[str, Any] | None:
    conn = connect(cfg)
    try:
        r = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    raw_path = r["path"]
    d = {k: (text(v) if isinstance(v, (bytes, bytearray)) else v) for k, v in dict(r).items()}
    d.pop("acoustid_fingerprint", None)
    d["lyrics"] = (d.get("lyrics") or "")[:4000]
    hp = host_path(cfg, raw_path)
    d["exists"] = bool(hp and hp.is_file())
    return d


# --------------------------------------------------------------------------
# facets
# --------------------------------------------------------------------------

def genres(cfg: HunterConfig, limit: int = 200) -> list[dict[str, Any]]:
    conn = connect(cfg)
    try:
        return [
            {"genre": r[0], "items": r[1], "albums": r[2]}
            for r in conn.execute(
                "SELECT genre, COUNT(*), COUNT(DISTINCT album_id) FROM items"
                " WHERE genre != '' GROUP BY genre ORDER BY COUNT(*) DESC LIMIT ?",
                (limit,),
            )
        ]
    finally:
        conn.close()


def albumtypes(cfg: HunterConfig) -> list[dict[str, Any]]:
    conn = connect(cfg)
    try:
        return [
            {"type": r[0] or "", "count": r[1]}
            for r in conn.execute(
                "SELECT albumtype, COUNT(*) FROM albums GROUP BY albumtype"
                " ORDER BY COUNT(*) DESC"
            )
        ]
    finally:
        conn.close()


def gap_rows(
    cfg: HunterConfig,
    *,
    coverage: float | None = None,
    min_missing: int = 2,
    min_expected: int = 4,
    max_expected: int = 40,
) -> list[tuple[int, str, str, int, int, int]]:
    """Incomplete albums in beets: ``(id, artist, album, year, have, expected)``.

    Uses ``_ALBUM_TOTAL_SUBQUERY`` so multi-disc albums with missing whole discs
    are detected accurately alongside partial discs.
    """
    cov = cfg.owned_coverage if coverage is None else float(coverage)
    conn = connect(cfg)
    try:
        rows = conn.execute(
            f"SELECT a.id, a.albumartist, a.album, COALESCE(a.year, 0) AS year,"
            f"       COUNT(i.id) AS have,"
            f"       COALESCE(MAX(_at.expected_tracks), 0) AS expected"
            f" FROM albums a"
            f" JOIN items i ON i.album_id = a.id"
            f" {_ALBUM_TOTAL_SUBQUERY}"
            f" WHERE a.albumartist NOT IN ('', 'Various Artists')"
            f"   AND LOWER(a.albumartist) != 'various artists'"
            f"   AND a.album != ''"
            f" GROUP BY a.id"
            f" HAVING expected BETWEEN ? AND ?"
            f"    AND have * 1.0 / expected < ?"
            f"    AND expected - have >= ?"
            f" ORDER BY have * 1.0 / expected, a.id",
            (min_expected, max_expected, cov, min_missing),
        ).fetchall()
        return [
            (
                int(r["id"]),
                str(r["albumartist"]),
                str(r["album"]),
                int(r["year"] or 0),
                int(r["have"]),
                int(r["expected"]),
            )
            for r in rows
        ]
    finally:
        conn.close()


def lossy_rows(
    cfg: HunterConfig,
    *,
    min_tracks: int = 4,
) -> list[tuple[int, str, str, int, int]]:
    """Zero-lossless albums in beets: ``(id, artist, album, year, n)``."""
    conn = connect(cfg)
    try:
        rows = conn.execute(
            f"SELECT a.id, a.albumartist, a.album, COALESCE(a.year, 0) AS year,"
            f"       COUNT(i.id) AS n"
            f" FROM albums a"
            f" JOIN items i ON i.album_id = a.id"
            f" WHERE a.albumartist NOT IN ('', 'Various Artists')"
            f"   AND LOWER(a.albumartist) != 'various artists'"
            f"   AND a.album != ''"
            f" GROUP BY a.id"
            f" HAVING n >= ?"
            f"    AND SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END) = 0"
            f" ORDER BY n DESC, a.id",
            (min_tracks,),
        ).fetchall()
        return [
            (
                int(r["id"]),
                str(r["albumartist"]),
                str(r["album"]),
                int(r["year"] or 0),
                int(r["n"]),
            )
            for r in rows
        ]
    finally:
        conn.close()


def pulse_counts(cfg: HunterConfig) -> dict[str, Any]:
    """Lightweight library counters for the Dashboard pulse endpoint."""
    conn = connect(cfg)
    try:
        conn.execute("BEGIN")
        albums_n = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        items_n, lossless_n = conn.execute(
            f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {_LOSSLESS_EXPR} THEN 1 ELSE 0 END), 0)"
            f" FROM items i"
        ).fetchone()
        recent_items = conn.execute(
            "SELECT COUNT(*) FROM items WHERE added > ?",
            (time.time() - 86_400,),
        ).fetchone()[0]
        return {
            "albums": int(albums_n or 0),
            "items": int(items_n or 0),
            "lossless_pct": round(100.0 * (lossless_n or 0) / items_n, 1) if items_n else 0.0,
            "added_24h": int(recent_items or 0),
        }
    finally:
        conn.close()


__all__ = [
    "album",
    "album_art_path",
    "album_groups",
    "album_ids",
    "albums",
    "albumtypes",
    "artist",
    "artist_groups",
    "artist_names",
    "artists",
    "connect",
    "duplicates",
    "gap_rows",
    "genres",
    "host_path",
    "junk",
    "lossy_rows",
    "overview",
    "pulse_counts",
    "text",
    "track",
    "track_groups",
    "track_ids",
    "tracks",
]

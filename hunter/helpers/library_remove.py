#!/usr/bin/env python3
"""Portable, self-contained journaled library removal & undo helper for beets.

Runs inside any stock beets container (no external repo dependencies — pure
Python stdlib, optional `beets` config auto-discovery):

  1. `--preflight` checks beets DB readability, music root availability,
     quarantine writability & free space, and optional filesystem type.
  2. Removal (`--artist` / `--album-id` / `--item-id`, `--apply`) moves every
     audio file (plus cover art & leftover sidecars when an album dir empties)
     into `<quarantine>/library_prune/<run_id>/`, writes a full JSON manifest
     with base64-preserved SQLite rows and flex attributes, and drops the rows
     from beets's SQLite DB atomically per item. Nothing is ever deleted.
  3. `--undo <manifest.json> --apply` moves quarantined files back to their
     exact original paths, re-inserts the exact `albums`, `items`,
     `album_attributes`, and `item_attributes` rows, and stamps `undone_ts`.

Emits progress lines (`[i/n] ...`) and a final `RESULT {json}` line parsed by
`hunter.libops`.
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HUGE = 5000
AUDIO_EXTS = frozenset({
    ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".aiff",
    ".aif", ".ape", ".wv", ".dsf", ".alac", ".wma", ".mpc",
})


def _dec(v) -> str:
    if v is None:
        return ""
    return v.decode("utf-8", "surrogateescape") if isinstance(v, (bytes, bytearray)) else str(v)


def _enc_row(row: sqlite3.Row) -> dict:
    out = {}
    for k in row.keys():
        v = row[k]
        if isinstance(v, (bytes, bytearray)):
            out[k] = {"__b64__": base64.b64encode(bytes(v)).decode("ascii")}
        else:
            out[k] = v
    return out


def _dec_row(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and "__b64__" in v:
            out[k] = base64.b64decode(v["__b64__"])
        else:
            out[k] = v
    return out


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "remove").lower()).strip("-")
    return (s or "remove")[:32]


def _emit_result(payload: dict) -> None:
    print("RESULT " + json.dumps(payload, ensure_ascii=False), flush=True)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".manifest.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _discover_db(explicit: str) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
    # Try beets's own config inside the container if installed.
    try:
        from beets import config as beets_config  # type: ignore
        lib_path = beets_config["library"].as_filename()
        if lib_path and Path(lib_path).is_file():
            return Path(lib_path)
    except Exception:
        pass
    for cand in (
        explicit,
        os.environ.get("BEETS_DB", ""),
        "/config/musiclibrary.db",
        "/beets/musiclibrary.db",
    ):
        if cand and Path(cand).is_file():
            return Path(cand)
    return Path(explicit or "/config/musiclibrary.db")


def _mount_fstype(target: Path) -> str:
    """Return the Linux mount fstype for *target*, or '' if unreadable."""
    try:
        resolved = os.path.realpath(str(target))
        best_mp = ""
        best_fs = ""
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mp = parts[1].replace("\\040", " ")
                if resolved == mp or resolved.startswith(mp.rstrip("/") + "/"):
                    if len(mp) >= len(best_mp):
                        best_mp = mp
                        best_fs = parts[2]
        return best_fs
    except OSError:
        return ""


def _preflight(db_path: Path, music_root: Path, quarantine_root: Path, required_fstype: str) -> dict:
    if not db_path.is_file():
        return {"ok": False, "error": f"beets DB not found: {db_path}"}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10.0)
        try:
            conn.execute("SELECT COUNT(*) FROM items").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"beets DB unreadable ({db_path}): {exc}"}

    if not music_root.is_dir():
        return {"ok": False, "error": f"music root missing: {music_root}"}
    try:
        if not any(music_root.iterdir()):
            return {"ok": False, "error": f"music root is empty (unmounted?): {music_root}"}
    except OSError as exc:
        return {"ok": False, "error": f"music root unreadable ({music_root}): {exc}"}

    if not quarantine_root.exists():
        try:
            quarantine_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"quarantine root unavailable ({quarantine_root}): {exc}"}
    if not os.access(str(quarantine_root), os.W_OK):
        return {"ok": False, "error": f"quarantine root not writable: {quarantine_root}"}

    if required_fstype:
        for label, p in (("music_root", music_root), ("quarantine", quarantine_root)):
            got = _mount_fstype(p)
            if got and got != required_fstype:
                return {
                    "ok": False,
                    "error": f"{label} fstype is {got!r}, expected {required_fstype!r}",
                }

    try:
        free_bytes = shutil.disk_usage(str(quarantine_root)).free
    except OSError as exc:
        return {"ok": False, "error": f"cannot stat quarantine free space: {exc}"}

    return {
        "ok": True,
        "db": str(db_path),
        "music_root": str(music_root),
        "quarantine": {"path": str(quarantine_root), "free_bytes": free_bytes},
    }


def _safe_rel(src: Path, music_root: Path) -> Path:
    try:
        return src.resolve().relative_to(music_root.resolve())
    except ValueError:
        parts = [p for p in src.parts if p not in ("/", "\\", "", "..", ".")]
        return Path(*parts[-3:]) if parts else Path(src.name or "file")


def _move_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stem, suf = dst.stem, dst.suffix
        dst = dst.with_name(f"{stem}_{secrets.token_hex(3)}{suf}")
    try:
        os.rename(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))


def _prune_empty_parents(start: Path, stop_at: Path) -> None:
    stop_res = stop_at.resolve()
    cur = start
    while True:
        try:
            if cur.resolve() == stop_res:
                return
            if not str(cur.resolve()).startswith(str(stop_res) + os.sep):
                return
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def _run_remove(
    db_path: Path,
    music_root: Path,
    quarantine_root: Path,
    required_fstype: str,
    *,
    artists: list[str],
    album_ids: list[int],
    item_ids: list[int],
    apply: bool,
    reason: str,
    requested_by: str,
    job_id: str,
    allow_huge: bool,
) -> int:
    pf = _preflight(db_path, music_root, quarantine_root, required_fstype)
    if not pf["ok"]:
        _emit_result(pf)
        return 1

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        target_albums: dict[int, sqlite3.Row] = {}
        target_items: dict[int, sqlite3.Row] = {}

        for name in artists:
            for r in conn.execute("SELECT * FROM albums WHERE albumartist=?", (name,)):
                target_albums[r["id"]] = r
            for r in conn.execute("SELECT * FROM items WHERE albumartist=?", (name,)):
                target_items[r["id"]] = r

        for aid in album_ids:
            r = conn.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
            if r:
                target_albums[r["id"]] = r
            for ir in conn.execute("SELECT * FROM items WHERE album_id=?", (aid,)):
                target_items[ir["id"]] = ir

        for iid in item_ids:
            ir = conn.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if ir:
                target_items[ir["id"]] = ir

        # Determine which touched albums lose all of their items.
        touched_aids = {r["album_id"] for r in target_items.values() if r["album_id"] is not None}
        full_album_ids: set[int] = set(target_albums.keys())
        for aid in touched_aids - full_album_ids:
            total = conn.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()[0]
            mine = sum(1 for r in target_items.values() if r["album_id"] == aid)
            if total > 0 and mine >= total:
                ar = conn.execute("SELECT * FROM albums WHERE id=?", (aid,)).fetchone()
                if ar:
                    target_albums[aid] = ar
                    full_album_ids.add(aid)

        if not target_items and not target_albums:
            _emit_result({"ok": False, "error": "no matching items or albums in beets DB"})
            return 1

        if len(target_items) > HUGE and not allow_huge:
            _emit_result({
                "ok": False,
                "error": f"refusing to remove {len(target_items)} tracks without --allow-huge",
            })
            return 1

        est_bytes = 0
        for r in target_items.values():
            p = Path(_dec(r["path"]))
            try:
                est_bytes += p.stat().st_size
            except OSError:
                pass

        if not apply:
            _emit_result({
                "ok": True,
                "dry_run": True,
                "tracks": len(target_items),
                "albums": len(full_album_ids),
                "bytes": est_bytes,
            })
            return 0

        free_bytes = pf["quarantine"]["free_bytes"]
        if free_bytes < est_bytes * 1.05:
            _emit_result({
                "ok": False,
                "error": f"insufficient free space on quarantine ({free_bytes} < {int(est_bytes * 1.05)})",
            })
            return 1

        label_seed = artists[0] if artists else (
            next((_dec(a["albumartist"]) for a in target_albums.values() if a["albumartist"]), "selection")
        )
        run_id = f"{_dt.datetime.now():%Y%m%d_%H%M%S}_{secrets.token_hex(2)}_{_slug(label_seed)}"
        run_dir = quarantine_root / "library_prune" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = run_dir / "manifest.json"

        plan_artists = sorted({
            _dec(r["albumartist"]) for r in target_items.values() if r["albumartist"]
        } | {
            _dec(a["albumartist"]) for a in target_albums.values() if a["albumartist"]
        })

        manifest: dict = {
            "id": run_id,
            "job_id": job_id,
            "ts": int(time.time()),
            "requested_by": requested_by,
            "reason": reason,
            "targets": {"artists": artists, "album_ids": album_ids, "item_ids": item_ids},
            "plan": {"artists": plan_artists, "bytes": est_bytes},
            "items": [],
            "albums": [],
            "extras": [],
            "errors": [],
            "blocked": [],
            "undone_ts": None,
        }
        _atomic_write_json(manifest_path, manifest)

        total_n = len(target_items)
        moved_count = 0
        moved_bytes = 0
        touched_dirs: set[Path] = set()

        for idx, (iid, row) in enumerate(sorted(target_items.items()), 1):
            src_str = _dec(row["path"])
            src = Path(src_str)
            rel = _safe_rel(src, music_root)
            dst = run_dir / "files" / rel
            attrs = [
                _enc_row(a)
                for a in conn.execute("SELECT key, value FROM item_attributes WHERE entity_id=?", (iid,))
            ]
            entry = {
                "item_id": iid,
                "src": src_str,
                "dst": str(dst),
                "moved": False,
                "error": None,
                "row": _enc_row(row),
                "attrs": attrs,
            }
            try:
                if src.is_file():
                    sz = src.stat().st_size
                    _move_file(src, dst)
                    entry["dst"] = str(dst)
                    entry["moved"] = True
                    moved_count += 1
                    moved_bytes += sz
                    touched_dirs.add(src.parent)
                conn.execute("DELETE FROM item_attributes WHERE entity_id=?", (iid,))
                conn.execute("DELETE FROM items WHERE id=?", (iid,))
                conn.commit()
            except (OSError, sqlite3.Error) as exc:
                entry["error"] = str(exc)
                manifest["errors"].append(f"item {iid}: {exc}")
            manifest["items"].append(entry)
            if idx % 20 == 0 or idx == total_n:
                print(f"[{idx}/{total_n}] moved {moved_count} files", flush=True)

        albums_removed = 0
        for aid in sorted(full_album_ids):
            ar = target_albums.get(aid)
            if ar is None:
                continue
            rem_items = conn.execute("SELECT COUNT(*) FROM items WHERE album_id=?", (aid,)).fetchone()[0]
            if rem_items > 0:
                continue
            a_attrs = [
                _enc_row(a)
                for a in conn.execute("SELECT key, value FROM album_attributes WHERE entity_id=?", (aid,))
            ]
            art_src = _dec(ar["artpath"])
            art_dst = ""
            if art_src and Path(art_src).is_file():
                adst = run_dir / "files" / _safe_rel(Path(art_src), music_root)
                try:
                    _move_file(Path(art_src), adst)
                    art_dst = str(adst)
                    touched_dirs.add(Path(art_src).parent)
                except OSError as exc:
                    manifest["errors"].append(f"art {aid}: {exc}")
            try:
                conn.execute("DELETE FROM album_attributes WHERE entity_id=?", (aid,))
                conn.execute("DELETE FROM albums WHERE id=?", (aid,))
                conn.commit()
                albums_removed += 1
                manifest["albums"].append({
                    "album_id": aid,
                    "row": _enc_row(ar),
                    "attrs": a_attrs,
                    "art_src": art_src or None,
                    "art_dst": art_dst or None,
                    "removed": True,
                })
            except sqlite3.Error as exc:
                manifest["errors"].append(f"album {aid}: {exc}")

        # Move non-audio leftovers out of now-audioless dirs and prune empty parents.
        for d in sorted(touched_dirs, key=lambda p: len(p.parts), reverse=True):
            if not d.is_dir():
                continue
            try:
                children = list(d.iterdir())
            except OSError:
                continue
            if any(c.is_file() and c.suffix.lower() in AUDIO_EXTS for c in children):
                continue
            if any(c.is_dir() for c in children):
                continue
            for c in children:
                if c.is_file():
                    edst = run_dir / "files" / _safe_rel(c, music_root)
                    try:
                        _move_file(c, edst)
                        manifest["extras"].append({"src": str(c), "dst": str(edst), "moved": True})
                    except OSError as exc:
                        manifest["errors"].append(f"extra {c.name}: {exc}")
            _prune_empty_parents(d, music_root)

        result = {
            "ok": len(manifest["errors"]) == 0,
            "run_dir": str(run_dir),
            "manifest": str(manifest_path),
            "moved": moved_count,
            "albums_removed": albums_removed,
            "bytes": moved_bytes,
            "errors": len(manifest["errors"]),
            "blocked": False,
        }
        manifest["result"] = result
        _atomic_write_json(manifest_path, manifest)
        _emit_result(result)
        return 0 if result["ok"] else 1
    finally:
        conn.close()


def _insert_row(conn: sqlite3.Connection, table: str, row_dict: dict) -> None:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    valid = [c for c in cols if c in row_dict]
    if not valid:
        return
    qm = ",".join("?" * len(valid))
    col_sql = ",".join(f'"{c}"' for c in valid)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({qm})",
        [row_dict[c] for c in valid],
    )


def _run_undo(db_path: Path, manifest_path: Path, *, apply: bool) -> int:
    if not manifest_path.is_file():
        _emit_result({"ok": False, "error": f"manifest not found: {manifest_path}"})
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit_result({"ok": False, "error": f"unreadable manifest: {exc}"})
        return 1
    if manifest.get("undone_ts"):
        _emit_result({"ok": False, "error": "removal already undone"})
        return 1
    if not apply:
        _emit_result({
            "ok": True,
            "dry_run": True,
            "items": len(manifest.get("items") or []),
            "albums": len(manifest.get("albums") or []),
        })
        return 0

    conn = sqlite3.connect(str(db_path), timeout=30.0)
    errors: list[str] = []
    files_restored = 0
    albums_restored = 0
    items_restored = 0
    try:
        for a in manifest.get("albums") or []:
            if not a.get("removed"):
                continue
            try:
                _insert_row(conn, "albums", _dec_row(a["row"]))
                aid = a["album_id"]
                for attr in a.get("attrs") or []:
                    d = _dec_row(attr)
                    conn.execute(
                        "INSERT INTO album_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                        (aid, d["key"], d["value"]),
                    )
                if a.get("art_src") and a.get("art_dst") and Path(a["art_dst"]).is_file():
                    _move_file(Path(a["art_dst"]), Path(a["art_src"]))
                    files_restored += 1
                albums_restored += 1
            except (OSError, sqlite3.Error) as exc:
                errors.append(f"album {a.get('album_id')}: {exc}")

        items = manifest.get("items") or []
        for idx, it in enumerate(items, 1):
            try:
                if it.get("moved") and it.get("dst") and Path(it["dst"]).is_file():
                    _move_file(Path(it["dst"]), Path(it["src"]))
                    files_restored += 1
                _insert_row(conn, "items", _dec_row(it["row"]))
                iid = it["item_id"]
                for attr in it.get("attrs") or []:
                    d = _dec_row(attr)
                    conn.execute(
                        "INSERT INTO item_attributes (entity_id, key, value) VALUES (?, ?, ?)",
                        (iid, d["key"], d["value"]),
                    )
                items_restored += 1
            except (OSError, sqlite3.Error) as exc:
                errors.append(f"item {it.get('item_id')}: {exc}")
            if idx % 20 == 0 or idx == len(items):
                print(f"[{idx}/{len(items)}] restored {files_restored} files", flush=True)

        for ex in manifest.get("extras") or []:
            if ex.get("moved") and ex.get("dst") and Path(ex["dst"]).is_file():
                try:
                    _move_file(Path(ex["dst"]), Path(ex["src"]))
                    files_restored += 1
                except OSError as exc:
                    errors.append(f"extra {ex.get('src')}: {exc}")

        conn.commit()
    finally:
        conn.close()

    if not errors:
        manifest["undone_ts"] = int(time.time())
        _atomic_write_json(manifest_path, manifest)

    res = {
        "ok": len(errors) == 0,
        "files": files_restored,
        "items_restored": items_restored,
        "albums_restored": albums_restored,
        "errors": len(errors),
        "error": "; ".join(errors[:5]) if errors else "",
    }
    _emit_result(res)
    return 0 if res["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Portable journaled beets library removal helper")
    ap.add_argument("--db", default="")
    ap.add_argument("--music-root", default="/music")
    ap.add_argument("--quarantine-root", default="/quarantine")
    ap.add_argument("--required-fstype", default="")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--undo", default="")
    ap.add_argument("--artist", action="append", default=[])
    ap.add_argument("--album-id", action="append", type=int, default=[])
    ap.add_argument("--item-id", action="append", type=int, default=[])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--block", action="store_true")
    ap.add_argument("--reason", default="")
    ap.add_argument("--requested-by", default="ui")
    ap.add_argument("--job-id", default="")
    ap.add_argument("--allow-huge", action="store_true")
    args = ap.parse_args(argv)

    db_path = _discover_db(args.db)
    music_root = Path(args.music_root)
    quarantine_root = Path(args.quarantine_root)

    if args.preflight:
        res = _preflight(db_path, music_root, quarantine_root, args.required_fstype.strip())
        _emit_result(res)
        return 0 if res["ok"] else 1

    if args.undo:
        return _run_undo(db_path, Path(args.undo), apply=args.apply)

    return _run_remove(
        db_path,
        music_root,
        quarantine_root,
        args.required_fstype.strip(),
        artists=args.artist,
        album_ids=args.album_id,
        item_ids=args.item_id,
        apply=args.apply,
        reason=args.reason,
        requested_by=args.requested_by,
        job_id=args.job_id,
        allow_huge=args.allow_huge,
    )


if __name__ == "__main__":
    sys.exit(main())

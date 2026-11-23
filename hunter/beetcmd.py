"""beet subcommand policy — what the console may run, and how.

The beets CLI is the management surface for the library; this module is
the fence around it. Every command the UI can fire is described here with
its class:

  read     inspects only (list, info, stats, missing, duplicates report…)
  mutate   changes tags / DB rows / file locations (modify, move, write,
           fetchart, embedart, mbsync, replaygain…)
  blocked  never runs from the UI: `remove` (deletes rows and, with -d,
           files), `import` (goes through the watcher's cascade instead —
           see beetsops.import_dir), `edit` (interactive).

Rules enforced by this module:
  * argv is a list — no shell, ever; raw text is shlex-split and each flag
    validated against the command's allowlist
  * `-y` is added only in APPLY mode. In dry-run mode the same command runs
    without it: stdin is /dev/null, so beets prints its plan and aborts at
    the confirmation prompt — a real preview of `modify` / `embedart`.
  * mutate commands need a blast-radius check: the UI previews
    `beet ls -f '$id' <query>`, and apply must echo the count back
    (`confirm_count`) whenever the query is empty or matches > SOFT_MAX.
  * `duplicates --delete/-d/--move/--copy`, `config -e`, and anything that
    deletes are blocked flags.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

BEET_ARGV = ["beet"]


def beet_argv(cfg: Any = None) -> list[str]:
    """Return the configured base argv for invoking beet."""
    if cfg is not None:
        cli = getattr(cfg, "beets_cli", None)
        if cli:
            if isinstance(cli, str):
                parts = shlex.split(cli)
                if parts:
                    return parts
            elif isinstance(cli, (list, tuple)):
                parts = [str(x) for x in cli if str(x)]
                if parts:
                    return parts
    return list(BEET_ARGV)

SOFT_MAX = 200          # above this, apply needs confirm_count
HARD_MAX = 20_000       # never from the UI without --allow-all + typed count


@dataclass(frozen=True)
class Flag:
    flag: str            # "-a" or "--force"
    label: str
    arg: str = ""        # non-empty = takes a value (label for the input)
    help: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Cmd:
    name: str
    title: str
    cls: str                       # read | mutate | blocked
    blurb: str
    query: str = "optional"        # required | optional | none
    flags: tuple[Flag, ...] = ()
    blocked: dict = field(default_factory=dict)   # flag -> reason
    confirm: tuple[str, ...] = ()  # flags added in apply mode (e.g. -y)
    pretend: tuple[str, ...] = ()  # flags injected in dry-run mode (e.g. -p)
    album_native: bool = False     # operates on albums even without -a
    positional: str = ""           # help for non-query positionals
    quick: bool = False            # may run attached (≤45s) for previews
    aliases: tuple[str, ...] = ()
    group: str = "library"


_ALBUM = Flag("-a", "Album mode", help="match/operate on albums instead of tracks")
_PATH = Flag("-p", "Show paths", help="print file paths instead of formatted rows")
_FMT = Flag("-f", "Format", arg="format string", help="e.g. '$artist - $album' or '$id'")
_PRETEND = Flag("-p", "Pretend", help="show what would change, change nothing")
_FORCE = Flag("-f", "Force", help="re-do even if data already present")
_NOMOVE = Flag("-M", "Don't move files", help="update tags/DB but leave paths alone")
_NOWRITE = Flag("-W", "Don't write tags", help="update DB only, leave files untouched")

COMMANDS: dict[str, Cmd] = {c.name: c for c in (
    # ---- read ---------------------------------------------------------
    Cmd("list", "List", "read", "Query the library. The building block: every other command takes the same query syntax.",
        flags=(_ALBUM, _PATH, _FMT), quick=True, aliases=("ls",), group="inspect"),
    Cmd("info", "Info", "read", "Show file tags vs library fields for matching items.",
        flags=(Flag("-l", "Library only", help="read DB fields, not file tags"),
               Flag("-s", "Summarize", help="one summary instead of per-item"),
               Flag("-i", "Include keys", arg="field list", help="comma-separated fields to show"),
               Flag("-f", "Format", arg="format string")),
        quick=True, group="inspect"),
    Cmd("stats", "Stats", "read", "Track/album/artist counts, total time and size for a query (or the whole library).",
        flags=(Flag("-e", "Exact", help="exact sizes — slow on USB, prefer a query"),),
        quick=True, group="inspect"),
    Cmd("fields", "Fields", "read", "Every queryable field name (items + albums).", query="none",
        quick=True, group="inspect"),
    Cmd("random", "Random", "read", "Pick random tracks/albums matching a query.",
        flags=(_ALBUM, Flag("-n", "Count", arg="n"), Flag("-e", "Equal artists", help="spread across artists"),
               Flag("-t", "Minutes", arg="minutes"), _PATH, _FMT),
        quick=True, group="inspect"),
    Cmd("missing", "Missing tracks", "read", "Albums with fewer tracks than MusicBrainz says (the gap-fill candidates).",
        flags=(_ALBUM, Flag("-c", "Count only"), Flag("-t", "Totals"), _FMT), group="inspect"),
    Cmd("duplicates", "Duplicates (report)", "read", "Tracks/albums sharing mb_trackid (or a chosen key). Report only — deletion is blocked; use Collapse duplicate masters.",
        flags=(_ALBUM,
               Flag("-k", "Keys", arg="fields", help="compare on these fields"),
               Flag("-F", "Full", help="show all copies, not just extras"),
               Flag("-s", "Strict", help="all keys must match"),
               Flag("-c", "Count"), _PATH, _FMT),
        blocked={"-C": "runs external command — disabled", "--checksum": "runs external command — disabled",
                 "-d": "deletes files — never from here", "--delete": "deletes files — never from here",
                 "-m": "moves files — use Collapse duplicate masters", "--move": "moves files — use Collapse duplicate masters",
                 "-o": "copies files — not from here", "--copy": "copies files — not from here",
                 "-r": "removes rows — never from here", "--remove": "removes rows — never from here",
                 "-M": "merges rows — not from here", "--merge": "merges rows — not from here",
                 "-t": "tag write from dupes — not from here", "--tag": "tag write from dupes — not from here"},
        aliases=("dup",), group="inspect"),
    Cmd("bad", "Corrupt-file check", "read", "Run the badfiles checker (flac/mp3 validators) over a query. Slow library-wide — scope it.",
        flags=(Flag("-v", "Verbose"),), group="inspect"),
    Cmd("unimported", "Untracked files", "read", "Files under the library root that beets doesn't know about.",
        query="none", flags=(Flag("-i", "Ignore extensions", arg="ext list"),), group="inspect"),
    Cmd("config", "Show config", "read", "Print the effective beets configuration.", query="none",
        flags=(Flag("-d", "Defaults", help="include defaults"), Flag("-p", "Paths", help="show config file paths")),
        blocked={"-e": "opens an editor — interactive", "--edit": "opens an editor — interactive"},
        quick=True, group="inspect"),
    Cmd("help", "Help", "read", "Detailed help for one subcommand.", query="none",
        positional="subcommand name", quick=True, group="inspect"),
    Cmd("version", "Version", "read", "beets + plugin versions.", query="none", quick=True, group="inspect"),
    # ---- mutate: metadata ----------------------------------------------
    Cmd("modify", "Modify fields", "mutate", "Set fields on matching items/albums: field=value, field! (unset). Writes tags and moves files unless told not to.",
        query="required", positional="field=value … (and field! to clear)",
        flags=(_ALBUM, _NOMOVE, _NOWRITE, Flag("-I", "Don't inherit", help="don't propagate album fields to its items")),
        confirm=("-y",), group="metadata"),
    Cmd("write", "Write tags", "mutate", "Write DB fields into file tags for matching items (after DB-only edits).",
        flags=(Flag("-p", "Pretend", help="show changes, write nothing"), Flag("-f", "Force", help="write even if tags look unchanged")),
        pretend=("-p",), group="metadata"),
    Cmd("update", "Update from files", "mutate", "Re-read file tags into the DB (the inverse of write). Picks up out-of-band tag edits.",
        flags=(_ALBUM, Flag("-p", "Pretend"), _NOMOVE, Flag("-F", "Fields", arg="field list", help="only these fields")),
        pretend=("-p",), group="metadata"),
    Cmd("mbsync", "MusicBrainz sync", "mutate", "Refresh tags from MusicBrainz for items that carry MB ids.",
        flags=(Flag("-p", "Pretend"), _NOMOVE, _NOWRITE), pretend=("-p",), group="metadata"),
    Cmd("scrub", "Scrub tags", "mutate", "Strip every tag from the files, then (with -W off) rewrite from the DB.",
        flags=(Flag("-W", "Don't rewrite", help="leave files tagless"),), group="metadata"),
    Cmd("ftintitle", "Feat → title", "mutate", "Move 'feat. X' from the artist field into the title.",
        flags=(Flag("-d", "Drop", help="remove feat. entirely instead of moving it"),), group="metadata"),
    Cmd("lastgenre", "Fetch genres", "mutate", "Last.fm genres for matching albums/items.",
        flags=(_ALBUM, _FORCE, Flag("-s", "Source", arg="artist|album|track")), group="metadata"),
    Cmd("lyrics", "Fetch lyrics", "mutate", "Fetch lyrics for matching items (network; scope it).",
        flags=(Flag("-p", "Print only", help="print, don't write"), _FORCE),
        pretend=("-p",), group="metadata"),
    Cmd("replaygain", "ReplayGain", "mutate", "Analyze loudness and store gain tags.",
        flags=(_ALBUM, _FORCE, Flag("-w", "Write tags"), Flag("-W", "Don't write")), group="metadata"),
    Cmd("fingerprint", "Fingerprint", "mutate", "Compute AcoustID fingerprints for items missing one (fpcalc).", group="metadata"),
    Cmd("submit", "Submit AcoustIDs", "mutate", "Upload fingerprints to AcoustID.", group="metadata"),
    # ---- mutate: art --------------------------------------------------
    Cmd("fetchart", "Fetch art", "mutate", "Download cover art for matching albums (CAA, iTunes, fanart…).",
        flags=(_FORCE, Flag("-q", "Quiet", help="only report new art")), album_native=True, group="art"),
    Cmd("embedart", "Embed art", "mutate", "Embed each album's art file into its tracks.",
        flags=(Flag("-f", "File", arg="image path", help="embed this file instead"),),
        confirm=("-y",), album_native=True, group="art"),
    Cmd("clearart", "Clear embedded art", "mutate", "Remove embedded images from matching files.",
        confirm=("-y",), group="art"),
    Cmd("extractart", "Extract art", "mutate", "Write embedded art out to a file next to the album.",
        flags=(Flag("-a", "Album (from file)", help="extract the album's art from a track"),
               Flag("-n", "Filename", arg="name"), Flag("-o", "Output", arg="path")),
        album_native=True, group="art"),
    # ---- mutate: files ------------------------------------------------
    Cmd("move", "Move / re-path", "mutate", "Move files to where the path templates say they belong (renormalize after tag edits).",
        flags=(_ALBUM, Flag("-p", "Pretend"), Flag("-t", "Timid", help="ask before each move — dry-run only"),
               Flag("-c", "Copy", help="copy instead of move")),
        blocked={"-d": "exporting to another dir — not from here", "--dest": "exporting to another dir — not from here",
                 "-e": "export mode — not from here", "--export": "export mode — not from here"},
        pretend=("-p",), group="files"),
    Cmd("convert", "Convert", "mutate", "Transcode matching items to the configured format/dest.",
        flags=(_ALBUM, Flag("-p", "Pretend"), Flag("-k", "Keep new", help="keep converted files in the library"),
               Flag("-f", "Format", arg="format"), Flag("-t", "Threads", arg="n")),
        confirm=("-y",), pretend=("-p",), group="files"),
    Cmd("splupdate", "Smart playlists", "mutate", "Regenerate the smart playlists.", query="none",
        album_native=True, group="files"),
    # ---- blocked -----------------------------------------------------
    Cmd("remove", "Remove", "blocked", "Deletes library rows (and files with -d). Never from the UI — use the recovery tools.", aliases=("rm",)),
    Cmd("import", "Import", "blocked", "Use Pipeline → Import a directory: it runs the watcher's cascade (autotag → resolver → quarantine), never a bare beet import.", aliases=("imp", "im")),
    Cmd("edit", "Edit (interactive)", "blocked", "Opens $EDITOR — use Modify fields instead."),
)}

_ALIASES = {a: c.name for c in COMMANDS.values() for a in c.aliases}


def resolve(name: str) -> Cmd | None:
    return COMMANDS.get(_ALIASES.get(name, name))


def catalogue() -> list[dict]:
    out = []
    for c in COMMANDS.values():
        out.append({
            "name": c.name, "title": c.title, "cls": c.cls, "blurb": c.blurb,
            "query": c.query, "group": c.group, "quick": c.quick,
            "positional": c.positional, "aliases": list(c.aliases),
            "confirm": list(c.confirm),
            "pretend": list(c.pretend),
            "can_dry_run": bool(c.cls == "read" or c.confirm or c.pretend),
            "album_native": c.album_native,
            "flags": [{"flag": f.flag, "label": f.label, "arg": f.arg,
                       "help": f.help} for f in c.flags],
            "blocked": {k: v for k, v in c.blocked.items() if v},
        })
    return out


class PolicyError(ValueError):
    pass


_MOD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:=.*|!)$")


def _is_modify_assignment(tok: str) -> bool:
    return bool(_MOD_RE.match(tok))


def _looks_like_flag(tok: str) -> bool:
    if not tok.startswith("-"):
        return False
    if tok in ("-", "--"):
        return True
    return not tok[1:].replace(".", "", 1).isdigit()


_DEFAULT_ALLOWED_ROOTS = ("/music", "/merged", "/quarantine")


def _norm_allowed_roots(allowed_roots: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if not allowed_roots:
        return _DEFAULT_ALLOWED_ROOTS
    out: list[str] = []
    for r in allowed_roots:
        if not isinstance(r, str):
            continue
        s = r.strip().rstrip("/")
        if s and s.startswith("/"):
            out.append(s)
    return tuple(dict.fromkeys(out)) or _DEFAULT_ALLOWED_ROOTS


def _is_confined_container_path(
    val: str,
    allowed_roots: tuple[str, ...] | list[str] | None = None,
) -> bool:
    if not val or "\x00" in val or "\\" in val:
        return False
    roots = _norm_allowed_roots(allowed_roots)
    if not any(val.startswith(r + "/") for r in roots):
        return False
    parts = PurePosixPath(val).parts
    return ".." not in parts and len(parts) >= 3


def _validate_flag_value(
    cmd: Cmd,
    flag: str,
    val: str,
    allowed_roots: tuple[str, ...] | list[str] | None = None,
) -> None:
    if "\x00" in val:
        raise PolicyError(f"{cmd.name} {flag}: NUL byte not allowed")
    if cmd.name == "extractart" and flag == "-n":
        if not val or "/" in val or "\\" in val or val in (".", ".."):
            raise PolicyError(f"{cmd.name} -n: filename cannot contain path separators")
    if (cmd.name == "extractart" and flag == "-o") or (cmd.name == "embedart" and flag == "-f"):
        if not _is_confined_container_path(val, allowed_roots):
            roots = " or ".join(r + "/" for r in _norm_allowed_roots(allowed_roots))
            raise PolicyError(
                f"{cmd.name} {flag}: path must be under {roots} without '..'"
            )


def _check_flag(
    cmd: Cmd,
    tok: str,
    *,
    allow_equals: bool = True,
    allow_confirm: bool = True,
    allowed_roots: tuple[str, ...] | list[str] | None = None,
) -> str:
    has_eq = "=" in tok
    if has_eq:
        if not allow_equals:
            raise PolicyError(f"{cmd.name}: flag {tok!r} cannot contain '='")
        base, val = tok.split("=", 1)
    else:
        base, val = tok, ""
    if base in cmd.blocked:
        why = cmd.blocked[base]
        if why:
            raise PolicyError(f"{cmd.name} {base}: {why}")
    if base in cmd.confirm:
        if has_eq:
            raise PolicyError(f"{cmd.name}: boolean flag {base!r} does not take a value")
        if not allow_confirm:
            raise PolicyError(f"{cmd.name}: confirmation flag {base!r} is only allowed in apply mode")
        return base
    spec = next((f for f in cmd.flags if f.flag == base), None)
    if spec is None:
        raise PolicyError(f"{cmd.name}: flag {base!r} is not enabled for the console")
    if has_eq:
        if not spec.arg:
            raise PolicyError(f"{cmd.name}: boolean flag {base!r} does not take a value")
        _validate_flag_value(cmd, base, val, allowed_roots=allowed_roots)
    return base


def _apply_mode_flags(cmd: Cmd, argv: list[str], apply: bool) -> None:
    if apply:
        for f in cmd.confirm:
            if f not in argv:
                argv.append(f)
        return
    if cmd.cls != "mutate":
        return
    if cmd.pretend:
        for f in reversed(cmd.pretend):
            if f not in argv:
                argv.insert(1, f)
    elif not cmd.confirm:
        raise PolicyError(f"{cmd.name} does not support dry-run (run with apply=True)")


def build(name: str, *, query: str = "", options: dict | None = None,
          positional: list[str] | None = None, apply: bool = False,
          for_preview: bool = False,
          allowed_roots: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """Structured build: options = {flag: True | value}."""
    cmd = resolve(name)
    if cmd is None:
        raise PolicyError(f"unknown beet subcommand {name!r}")
    if cmd.cls == "blocked":
        raise PolicyError(f"{cmd.name}: {cmd.blurb}")
    argv = [cmd.name]
    for flag, val in (options or {}).items():
        if val is False or val is None or val == "":
            continue
        _check_flag(cmd, str(flag), allow_equals=False, allow_confirm=apply,
                    allowed_roots=allowed_roots)
        spec = next((f for f in cmd.flags if f.flag == flag), None)
        if spec and spec.arg:
            sval = str(val)
            _validate_flag_value(cmd, flag, sval, allowed_roots=allowed_roots)
            argv += [flag, sval]
        elif flag not in cmd.confirm:
            argv.append(flag)
    if not for_preview:
        _apply_mode_flags(cmd, argv, apply)
    q_tokens = shlex.split(query) if query.strip() else []
    if cmd.query == "required" and not q_tokens:
        raise PolicyError(f"{cmd.name} needs a query — an empty query would touch the whole library")
    if cmd.query == "none" and q_tokens:
        raise PolicyError(f"{cmd.name} takes no query")
    for tok in q_tokens:
        if _looks_like_flag(tok):
            raise PolicyError(f"query token {tok!r} looks like a flag — use the option toggles")
    pos_tokens: list[str] = []
    for p in positional or []:
        s = str(p).strip()
        if not s:
            continue
        if s.startswith("-"):
            raise PolicyError(f"positional token {s!r} looks like a flag")
        pos_tokens.append(s)
    argv += q_tokens
    argv += pos_tokens
    return argv


def build_raw(raw: str, apply: bool = False, *, for_preview: bool = False,
              allowed_roots: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """Free-text build: `modify -a artist:Foo album:Bar year=1999`."""
    toks = shlex.split(raw.strip())
    if not toks:
        raise PolicyError("empty command")
    if toks[0] == "beet":
        toks = toks[1:]
    if not toks:
        raise PolicyError("no subcommand")
    cmd = resolve(toks[0])
    if cmd is None:
        raise PolicyError(f"unknown beet subcommand {toks[0]!r}")
    if cmd.cls == "blocked":
        raise PolicyError(f"{cmd.name}: {cmd.blurb}")
    argv = [cmd.name]
    rest = toks[1:]
    i = 0
    query_seen = False
    pos_seen = 0
    while i < len(rest):
        t = rest[i]
        if not t:
            raise PolicyError("empty token in command")
        if _looks_like_flag(t):
            if t in cmd.confirm:
                if not apply:
                    raise PolicyError(
                        f"{cmd.name}: confirmation flag {t!r} is only allowed in apply mode")
                i += 1
                continue          # we own -y
            base = _check_flag(cmd, t, allow_equals=True, allow_confirm=apply,
                               allowed_roots=allowed_roots)
            spec = next((f for f in cmd.flags if f.flag == base), None)
            argv.append(t)
            if spec and spec.arg and "=" not in t:
                if i + 1 >= len(rest):
                    raise PolicyError(f"{base} needs a value")
                _validate_flag_value(cmd, base, rest[i + 1], allowed_roots=allowed_roots)
                argv.append(rest[i + 1])
                i += 2
                continue
            i += 1
            continue
        if cmd.query == "none":
            if not cmd.positional:
                raise PolicyError(f"{cmd.name} takes no query")
            pos_seen += 1
            if pos_seen > 1:
                raise PolicyError(f"{cmd.name} takes at most one positional argument")
        elif not (cmd.name == "modify" and _is_modify_assignment(t)):
            query_seen = True
        argv.append(t)
        i += 1
    if cmd.query == "required" and not query_seen:
        raise PolicyError(f"{cmd.name} needs a query — an empty query would touch the whole library")
    if not for_preview:
        _apply_mode_flags(cmd, argv, apply)
    return argv


def query_of(argv: list[str]) -> tuple[Cmd | None, list[str], bool]:
    """(cmd, query tokens, album_mode) from a built argv — for the
    blast-radius preview. Mods (field=value / field!) are excluded."""
    if not argv:
        return None, [], False
    cmd = resolve(argv[0])
    if cmd is None:
        return None, [], False
    album = ("-a" in argv[1:]) or cmd.album_native
    toks: list[str] = []
    skip = False
    valued = {f.flag for f in cmd.flags if f.arg}
    for t in argv[1:]:
        if skip:
            skip = False
            continue
        if _looks_like_flag(t):
            if t.split("=", 1)[0] in valued and "=" not in t:
                skip = True
            continue
        if cmd.name == "modify" and _is_modify_assignment(t):
            continue
        toks.append(t)
    return cmd, toks, album


def preview_argv(query_tokens: list[str], album: bool) -> list[str]:
    """`beet ls [-a] -f '$id' <query>` — the count that gates apply."""
    argv = ["list"]
    if album:
        argv.append("-a")
    argv += ["-f", "$id\t$albumartist\t$album" if album
             else "$id\t$artist\t$album\t$title"]
    argv += query_tokens
    return argv


def needs_confirm(cmd: Cmd, query_tokens: list[str], count: int,
                  argv: list[str]) -> bool:
    if cmd.cls != "mutate":
        return False
    if "-p" in argv and cmd.name in ("write", "update", "mbsync", "move",
                                      "convert", "lyrics"):
        return False          # pretend modes change nothing
    return not query_tokens or count > SOFT_MAX

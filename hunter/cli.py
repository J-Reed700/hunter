"""hunter CLI — thin operator & entrypoint adapter.

    hunter pass [--apply|--dry-run] [--limit N]
                [--skip-spotify] [--skip-hunt] [--skip-playlists] [--skip-artists]
    hunter add "Artist" "Album" [--force]
    hunter drop "Artist" ["Album"] [--source SRC]
    hunter status
    hunter seed-gaps / seed-upgrades
    hunter auth
    hunter user list|add|passwd|role|enable|disable|delete|revoke
    hunter health
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import time
import urllib.parse
from datetime import datetime

from hunter import blocklist, config, domain, pipeline, storage
from hunter.auth import ROLES, Auth, AuthError
from hunter.config import HunterConfig
from hunter.http_client import HttpError, HttpSession, default_user_agent
from hunter.library import BeetsLibrary
from hunter.state import HunterState

log = logging.getLogger("hunter")


def _setup_logging(cfg: HunterConfig, verbose: bool) -> None:
    """stdout + daily file under /state/logs, self-pruned and deduplicated."""
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if getattr(root, "_hunter_configured", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(cfg.log_dir / f"hunter_{datetime.now():%Y%m%d}.log"),
    ):
        h.setFormatter(fmt)
        root.addHandler(h)
    setattr(root, "_hunter_configured", True)

    cutoff = time.time() - cfg.log_keep_days * 86_400
    for old in cfg.log_dir.glob("hunter_*.log"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def cmd_pass(cfg: HunterConfig, args: argparse.Namespace) -> int:
    apply = True if args.apply else (False if args.dry_run else None)
    return pipeline.run_pass(
        cfg,
        apply=apply,
        limit=args.limit or None,
        verbose=args.verbose,
        skip_spotify=args.skip_spotify,
        skip_hunt=args.skip_hunt,
        skip_playlists=args.skip_playlists,
        skip_artists=args.skip_artists,
    )


def cmd_add(cfg: HunterConfig, args: argparse.Namespace) -> int:
    if blocklist.is_blocked(args.artist, cfg):
        print(f"blocked by artist blocklist: {args.artist}")
        return 1

    if not args.force:
        try:
            with BeetsLibrary(cfg.beets_db) as lib:
                hit = lib.already_have(
                    args.artist, args.album, min_coverage=cfg.owned_coverage
                )
            if hit is not None:
                print(
                    f"already owned: {args.artist} — {args.album} "
                    f"(beets album {hit.album_id}, {hit.item_count} tracks; "
                    "pass --force to queue anyway)"
                )
                return 0
        except Exception as exc:                   # noqa: BLE001
            log.debug("owned-gate skipped (%s)", exc)

    key = f"manual:{domain.norm_artist(args.artist)}:{domain.norm_album(args.album)}"
    with HunterState(cfg.state_db) as state:
        is_new = state.upsert_want(
            key, args.artist, args.album, 0, None, "manual", resurrect=True
        )
        state.log_event("request", args.artist, args.album, "added via cli")
        counts = state.counts()
    print(
        f"{'added' if is_new else 'already tracked'}: "
        f"{args.artist} — {args.album}\ncounts: {counts}"
    )
    return 0


def cmd_drop(cfg: HunterConfig, args: argparse.Namespace) -> int:
    if not args.artist and not args.album and args.source == "%":
        print('usage: hunter drop "Artist" ["Album"] [--source SRC] (SQL LIKE patterns)')
        return 2
    with HunterState(cfg.state_db) as state:
        dropped = state.drop_wants(
            args.artist or "%",
            args.album or "%",
            args.source,
            literal=False,
        )
        counts = state.counts()
    print(
        f"dropped {dropped} want(s) matching "
        f"{args.artist!r} — {args.album!r} (source {args.source!r})\ncounts: {counts}"
    )
    return 0


def cmd_seed_gaps(cfg: HunterConfig, args: argparse.Namespace) -> int:
    rows = pipeline.gap_rows(
        cfg,
        args.coverage,
        args.min_missing,
        args.min_expected,
        args.max_expected,
    )
    if args.seed_limit:
        rows = rows[:args.seed_limit]
    missing_total = sum(r[5] - r[4] for r in rows)
    if args.dry_run:
        print(
            f"[dry-run] would seed {len(rows)} partial albums "
            f"({missing_total} missing tracks). Worst 10:"
        )
        for r in rows[:10]:
            print(f"  {r[1]} — {r[2]} ({r[4]}/{r[5]} tracks)")
        return 0
    with HunterState(cfg.state_db) as state:
        new = pipeline.seed_gaps(
            cfg,
            state,
            coverage=args.coverage,
            min_missing=args.min_missing,
            min_expected=args.min_expected,
            max_expected=args.max_expected,
            limit=args.seed_limit,
            apply=True,
        )
        counts = state.counts()
    print(
        f"seeded {new} new gap-fill wants ({len(rows)} qualifying partial albums, "
        f"{missing_total} missing tracks)\ncounts: {counts}"
    )
    return 0


def cmd_seed_upgrades(cfg: HunterConfig, args: argparse.Namespace) -> int:
    rows = pipeline.lossy_rows(cfg, args.min_tracks)
    if args.seed_limit:
        rows = rows[:args.seed_limit]
    if args.dry_run:
        print(
            f"[dry-run] would seed {len(rows)} lossy-only albums for "
            "lossless upgrade. First 10:"
        )
        for r in rows[:10]:
            print(f"  {r[1]} — {r[2]} ({r[4]} tracks)")
        return 0
    with HunterState(cfg.state_db) as state:
        new = pipeline.seed_upgrades(
            cfg, state, min_tracks=args.min_tracks, limit=args.seed_limit, apply=True
        )
        counts = state.counts()
    print(f"seeded {new} upgrade wants ({len(rows)} lossy-only albums)\ncounts: {counts}")
    return 0


def cmd_status(cfg: HunterConfig, _args: argparse.Namespace) -> int:
    with HunterState(cfg.state_db) as state:
        print(
            f"apply mode: {cfg.apply}   spotify: "
            f"{'configured' if cfg.spotify_configured else 'NOT CONFIGURED'}   "
            f"slskd: {'configured' if cfg.slskd_configured else 'NOT CONFIGURED'}"
        )
        print(f"counts: {state.counts()}")
        for row in state.recent_open(limit=40):
            print(
                f"  [{row['status']:>9}] {row['artist']} — {row['album']}"
                f"  (src={row['source']}, attempts={row['attempts']}, {row['upd']})"
            )
    return 0


def cmd_auth(cfg: HunterConfig, _args: argparse.Namespace) -> int:
    """Interactive one-time Spotify refresh-token mint."""
    if not cfg.spotify_client_id or not cfg.spotify_client_secret:
        print(
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set — put them in"
            " .env, `docker compose up -d hunter`, then rerun."
        )
        return 2

    redirect = "http://127.0.0.1:8888/callback"
    query = urllib.parse.urlencode({
        "client_id": cfg.spotify_client_id,
        "response_type": "code",
        "redirect_uri": redirect,
        "scope": "playlist-read-private playlist-read-collaborative user-library-read",
    })
    print("\n1. Open this URL in a browser and approve access:\n")
    print(f"   https://accounts.spotify.com/authorize?{query}\n")
    print("2. You'll land on a 127.0.0.1 URL that won't load — that's fine.")
    pasted = input("3. Paste that full redirect URL here: ").strip()
    code = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query).get("code", [""])[0]
    if not code:
        print("no ?code= found in that URL")
        return 2

    with HttpSession(
        timeout=30,
        user_agent=default_user_agent(cfg),
        proxy=cfg.spotify_proxy or None,
    ) as http:
        try:
            resp = http.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
                auth=(cfg.spotify_client_id, cfg.spotify_client_secret),
                timeout=30,
                raise_for_status=False,
            )
        except HttpError as exc:
            print(f"token exchange failed: {exc}")
            return 1
    if resp.status_code != 200:
        print(f"token exchange failed: {resp.status_code} {resp.text[:300]}")
        return 1
    try:
        tok = resp.json()
    except ValueError as exc:
        print(f"token exchange returned invalid JSON: {exc}")
        return 1
    if not isinstance(tok, dict) or not tok.get("refresh_token"):
        print(f"no refresh_token in response: {tok}")
        return 1

    config.save_spotify_token(cfg.token_file, {"refresh_token": tok["refresh_token"]})
    print(f"\nrefresh token saved → {cfg.token_file}")
    print("passes will sync Spotify from here on. Done.")
    return 0


def cmd_user(cfg: HunterConfig, args: argparse.Namespace) -> int:
    """Account recovery + management from the shell."""
    def read_pw() -> str:
        if getattr(args, "password", None):
            return args.password
        if sys.stdin.isatty():
            a = getpass.getpass("password: ")
            b = getpass.getpass("again: ")
            if a != b:
                print("passwords differ")
                raise SystemExit(2)
            return a
        return sys.stdin.readline().rstrip("\n")

    sub = args.user_cmd
    name = getattr(args, "username", "") or ""
    with Auth(cfg.state_db) as a:
        try:
            if sub == "list":
                rows = a.users()
                if not rows:
                    print("no users — the UI is in setup mode")
                for u in rows:
                    flags = []
                    if u["disabled"]:
                        flags.append("disabled")
                    if u["must_change_pw"]:
                        flags.append("must-change-pw")
                    print(
                        f"{u['id']:>3}  {u['username']:<20} {u['role']:<9}"
                        f" sessions={u['sessions']} {' '.join(flags)}"
                    )
                return 0
            if sub == "add":
                role = args.role or ("admin" if a.setup_required else "viewer")
                u = a.create_user(name, read_pw(), role, created_by="cli")
                a.audit(None, "cli.user.add", u["username"], f"role={u['role']}", "cli")
                print(f"created {u['username']} ({u['role']})")
                return 0
            u = a.user_by_name(name)
            if not u:
                print(f"no such user {name!r}")
                return 1
            if sub == "passwd":
                a.set_password(u["id"], read_pw())
                print(f"password set for {name}; all sessions revoked")
            elif sub == "role":
                role = args.role_flag or args.role_pos
                if role not in ROLES:
                    print(f"role must be one of {', '.join(ROLES)}")
                    return 2
                a.update_user(u["id"], role=role)
                print(f"{name} is now {role}")
            elif sub == "enable":
                a.update_user(u["id"], disabled=False)
                print(f"{name} enabled")
            elif sub == "disable":
                a.update_user(u["id"], disabled=True)
                print(f"{name} disabled; sessions revoked")
            elif sub == "delete":
                a.delete_user(u["id"])
                print(f"{name} deleted")
            elif sub == "revoke":
                n = a.revoke_all(u["id"])
                print(f"{n} session(s) revoked for {name}")
            a.audit(None, f"cli.user.{sub}", name, "via hunter CLI", "cli")
        except AuthError as exc:
            print(f"error: {exc}")
            return 1
    return 0


def cmd_health(cfg: HunterConfig) -> int:
    """Compose healthcheck (`hunter health`). Runs BEFORE logging setup."""
    problems: list[str] = []
    now = time.time()
    try:
        hb = json.loads((cfg.state_dir / "heartbeat.json").read_text(encoding="utf-8"))
        if not isinstance(hb, dict):
            hb = {}
    except (OSError, ValueError):
        hb = {}
    limit = 2 * cfg.interval + cfg.pass_deadline
    age = now - float(hb.get("ts") or 0)
    if age > limit:
        problems.append(f"heartbeat {int(age)}s old (limit {limit}s)")
    if hb.get("status") == "crashed":
        problems.append(f"last pass crashed: {hb.get('error', 'unknown')}")

    try:
        sup = json.loads((cfg.state_dir / "supervisor.json").read_text(encoding="utf-8"))
        if not isinstance(sup, dict):
            sup = {}
    except (OSError, ValueError):
        sup = {}
    failures = int(sup.get("consecutive_failures") or 0)
    busy_streak = int(sup.get("lock_busy_streak") or 0)
    if failures >= pipeline.FAILURE_ALERT_AFTER:
        problems.append(f"{failures} consecutive pass failures (last rc={sup.get('rc')})")
    if busy_streak >= pipeline.FAILURE_ALERT_AFTER + 1:
        problems.append(f"pass lock busy for {busy_streak} consecutive cycles")

    st = storage.summary(cfg)
    if not st["ok"]:
        problems.append(st["error"])

    print(
        json.dumps({
            "ok": not problems,
            "problems": problems,
            "heartbeat_age_s": int(age),
            "consecutive_failures": failures,
            "lock_busy_streak": busy_streak,
            "storage": {
                k: v.get("live")
                for k, v in st.items()
                if isinstance(v, dict)
            },
        })
    )
    return 0 if not problems else 1


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hunter",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command")

    p_pass = sub.add_parser("pass", help="run one orchestration pass")
    mode = p_pass.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="force apply regardless of HUNTER_APPLY")
    mode.add_argument("--dry-run", action="store_true", help="force shadow mode regardless of HUNTER_APPLY")
    p_pass.add_argument("--limit", type=int, default=0, help="max grabs this pass")
    p_pass.add_argument("--skip-spotify", action="store_true")
    p_pass.add_argument("--skip-hunt", action="store_true")
    p_pass.add_argument("--skip-playlists", action="store_true")
    p_pass.add_argument("--skip-artists", action="store_true")
    p_pass.add_argument("-v", "--verbose", action="store_true")

    p_add = sub.add_parser("add", help="add a manual want")
    p_add.add_argument("artist")
    p_add.add_argument("album")
    p_add.add_argument("--force", action="store_true", help="queue even if already owned")
    p_add.add_argument("-v", "--verbose", action="store_true")

    p_drop = sub.add_parser("drop", help="un-want matching SQL LIKE patterns")
    p_drop.add_argument("artist", nargs="?", default="")
    p_drop.add_argument("album", nargs="?", default="")
    p_drop.add_argument("--source", default="%")
    p_drop.add_argument("-v", "--verbose", action="store_true")

    p_gaps = sub.add_parser("seed-gaps", help="seed wants from partial beets albums")
    p_gaps.add_argument("--coverage", type=float, default=None)
    p_gaps.add_argument("--min-missing", type=int, default=2)
    p_gaps.add_argument("--min-expected", type=int, default=4)
    p_gaps.add_argument("--max-expected", type=int, default=40)
    p_gaps.add_argument("--seed-limit", type=int, default=0)
    p_gaps.add_argument("--dry-run", action="store_true")
    p_gaps.add_argument("-v", "--verbose", action="store_true")

    p_upg = sub.add_parser("seed-upgrades", help="seed lossless upgrade wants")
    p_upg.add_argument("--min-tracks", type=int, default=4)
    p_upg.add_argument("--seed-limit", type=int, default=0)
    p_upg.add_argument("--dry-run", action="store_true")
    p_upg.add_argument("-v", "--verbose", action="store_true")

    p_status = sub.add_parser("status", help="print want counts and open wants")
    p_status.add_argument("-v", "--verbose", action="store_true")

    p_auth = sub.add_parser("auth", help="interactive Spotify OAuth refresh-token mint")
    p_auth.add_argument("-v", "--verbose", action="store_true")

    p_health = sub.add_parser("health", help="Docker compose healthcheck JSON")
    p_health.add_argument("-v", "--verbose", action="store_true")

    p_user = sub.add_parser("user", help="manage Web UI user accounts")
    p_user.add_argument("-v", "--verbose", action="store_true")
    usub = p_user.add_subparsers(dest="user_cmd", required=True)
    usub.add_parser("list")
    u_add = usub.add_parser("add")
    u_add.add_argument("username")
    u_add.add_argument("--role", choices=ROLES, default=None)
    u_add.add_argument("--password", default=None)
    u_pw = usub.add_parser("passwd")
    u_pw.add_argument("username")
    u_pw.add_argument("--password", default=None)
    u_role = usub.add_parser("role")
    u_role.add_argument("username")
    u_role.add_argument("role_pos", nargs="?", choices=ROLES, default=None)
    u_role.add_argument("--role", dest="role_flag", choices=ROLES, default=None)
    for verb in ("enable", "disable", "delete", "revoke"):
        u_v = usub.add_parser(verb)
        u_v.add_argument("username")

    return ap


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv or (raw_argv[0].startswith("-") and raw_argv[0] not in ("-h", "--help")):
        raw_argv = ["pass", *raw_argv]

    ap = _build_parser()
    args = ap.parse_args(raw_argv)
    cfg = HunterConfig.from_env()

    if args.command == "health":
        return cmd_health(cfg)

    _setup_logging(cfg, getattr(args, "verbose", False))

    dispatch = {
        "pass": cmd_pass,
        "add": cmd_add,
        "drop": cmd_drop,
        "seed-gaps": cmd_seed_gaps,
        "seed-upgrades": cmd_seed_upgrades,
        "status": cmd_status,
        "auth": cmd_auth,
        "user": cmd_user,
    }
    return dispatch[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())

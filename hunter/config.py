"""Hunter settings — env-driven, one dataclass, no surprises.

Container filesystem contract (see docker-compose.yml):
    /state   hunter's own volume — wants DB, spotify token, heartbeat, logs
    /beets   the beets config dir, for musiclibrary.db. Mounted rw ONLY
             because SQLite WAL readers need the -shm sidecar file; every
             connection hunter opens is `mode=ro`. hunter never writes here.

The one knob that matters operationally: HUNTER_APPLY — 0 (default) is
shadow mode: full Spotify sync + beets diff + real slskd searches, but
nothing is enqueued and no playlist is written. Set 1 in .env to go live.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shlex
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from hunter import lock
from hunter.domain import atomic_write_json, atomic_write_text

log = logging.getLogger("hunter.config")

_VALID_WANT_SOURCES = frozenset({"saved_albums", "playlists"})
_ENV_PREFIXES = (
    "HUNTER_",
    "SLSKD_",
    "NAVIDROME_",
    "PROWLARR_",
    "SPOTIFY_",
    "AUDIOMUSE_",
    "NOTIFY_",
    "BEETS_",
    "DOCKER_SOCK",
)
_DEFAULT_TRUSTED_PROXIES = (
    "127.0.0.0/8",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)
_DEFAULT_BACKOFF = (86_400, 259_200, 604_800)

_WRITE_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: tuple[tuple[Any, ...], HunterConfig] | None = None


def _env_str(name: str, default: str) -> str:
    # Empty or whitespace-only == unset: compose passes `${VAR:-}`
    # placeholders through as "" — use default rather than blanking.
    raw = os.environ.get(name)
    if raw is None:
        return default
    stripped = raw.strip()
    return stripped if stripped else default


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return default if (value < minimum or value > maximum) else value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < minimum or value > maximum:
        return default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _env_int_tuple(
    name: str,
    default: tuple[int, ...],
    minimum: int = 0,
    maximum: int = 30 * 86_400,
) -> tuple[int, ...]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            return default
        if v < minimum or v > maximum:
            return default
        out.append(v)
    return tuple(out) if out else default


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    items = tuple(p.strip() for p in raw.split(",") if p.strip())
    return items if items else default


def _read_json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_apply(state_dir: Path) -> bool:
    """Live-togglable apply mode: /state/apply_override beats HUNTER_APPLY."""
    override = state_dir / "apply_override"
    try:
        if override.exists():
            return override.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        pass
    return _env_bool("HUNTER_APPLY", False)


def _read_spotify_app(state_dir: Path) -> tuple[str, str]:
    app = _read_json_dict(state_dir / "spotify_app.json")
    cid = app.get("client_id")
    sec = app.get("client_secret")
    if isinstance(cid, str) and isinstance(sec, str):
        cid_s, sec_s = cid.strip(), sec.strip()
        if cid_s and sec_s:
            return cid_s, sec_s
    return "", ""


def _stat_sig(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def clear_config_cache() -> None:
    """Reset the in-process config cache (primarily for tests)."""
    global _CONFIG_CACHE
    with _CACHE_LOCK:
        _CONFIG_CACHE = None


def parse_tunable(key: str, raw: Any) -> int | bool:
    """Validate and coerce a single ``UI_TUNABLE`` setting.

    Rejects unknown keys, booleans for integer fields, non-integer floats,
    and out-of-range values with ``ValueError``.
    """
    if key not in HunterConfig.UI_TUNABLE:
        raise ValueError(f"unknown tunable: {key}")
    spec = HunterConfig.UI_TUNABLE[key]
    if spec is None:
        if isinstance(raw, bool):
            return raw
        raise ValueError(f"{key} must be a boolean")
    if isinstance(raw, bool):
        raise ValueError(f"{key} must be an integer, not bool")
    if isinstance(raw, int):
        val = raw
    elif isinstance(raw, float):
        if not math.isfinite(raw) or not raw.is_integer():
            raise ValueError(f"{key} must be a whole integer")
        val = int(raw)
    elif isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise ValueError(f"{key} cannot be empty")
        try:
            val = int(s)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
    else:
        raise ValueError(f"{key} must be an integer")
    lo, hi = spec
    if not (lo <= val <= hi):
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return val


def save_tunables(cfg: "HunterConfig", patch: dict[str, Any]) -> dict[str, int | bool]:
    """Validate *patch* against ``UI_TUNABLE`` and atomically persist to
    ``settings.json``. Returns the dict of keys actually applied."""
    target = cfg.state_dir / "settings.json"
    with _WRITE_LOCK, lock.hold(cfg.state_dir / "settings.lock", blocking=True):
        current = _read_json_dict(target)
        applied: dict[str, int | bool] = {}
        for key, raw in patch.items():
            if key not in HunterConfig.UI_TUNABLE:
                continue
            try:
                val = parse_tunable(key, raw)
            except ValueError as exc:
                log.warning("ignoring invalid tunable %s: %s", key, exc)
                continue
            current[key] = applied[key] = val
        if applied:
            atomic_write_json(target, current, indent=2)
            clear_config_cache()
        return applied


def save_apply_override(cfg: "HunterConfig", enabled: bool) -> None:
    """Atomically write ``apply_override`` ('1' or '0')."""
    target = cfg.state_dir / "apply_override"
    with _WRITE_LOCK:
        atomic_write_text(target, "1" if enabled else "0")
        clear_config_cache()


def save_spotify_app(
    cfg: "HunterConfig",
    client_id: str,
    client_secret: str,
) -> None:
    """Atomically persist ``spotify_app.json`` with ``0o600`` permissions."""
    cid = (client_id or "").strip()
    sec = (client_secret or "").strip()
    if not cid or not sec:
        raise ValueError("client_id and client_secret must be non-empty")
    target = cfg.state_dir / "spotify_app.json"
    with _WRITE_LOCK:
        atomic_write_json(
            target,
            {"client_id": cid, "client_secret": sec},
            mode=0o600,
            indent=2,
        )
        clear_config_cache()


def save_spotify_token(
    target: "Path | HunterConfig",
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Atomically merge-write ``spotify_token.json`` with ``0o600`` permissions
    under a cross-process flock so ``sp_dc`` and ``refresh_token`` never clobber
    each other across web UI and CLI/pass processes."""
    token_path = target.token_file if isinstance(target, HunterConfig) else Path(target)
    lock_path = token_path.parent / f".{token_path.name}.lock"
    with _WRITE_LOCK, lock.hold(lock_path, blocking=True):
        current = _read_json_dict(token_path)
        merged = {**current, **updates}
        atomic_write_json(token_path, merged, mode=0o600, indent=2)
        clear_config_cache()
        return merged


def delete_spotify_token(target: "Path | HunterConfig") -> None:
    """Remove ``spotify_token.json`` under the same cross-process flock."""
    token_path = target.token_file if isinstance(target, HunterConfig) else Path(target)
    lock_path = token_path.parent / f".{token_path.name}.lock"
    with _WRITE_LOCK, lock.hold(lock_path, blocking=True):
        try:
            token_path.unlink()
        except FileNotFoundError:
            pass
        clear_config_cache()


# --- Normalized beets-container accessors (also accept SimpleNamespace) ---

def beets_python_bin(cfg: Any = None) -> str:
    val = getattr(cfg, "beets_python", None) if cfg is not None else None
    return val.strip() if isinstance(val, str) and val.strip() else "python3"


def beets_automation_dir_str(cfg: Any = None) -> str:
    val = getattr(cfg, "beets_automation_container_dir", None) if cfg is not None else None
    if isinstance(val, str) and val.strip():
        return val.strip().rstrip("/") or "/config/automation"
    return "/config/automation"


def beets_quarantine_root_str(cfg: Any = None) -> str:
    val = getattr(cfg, "beets_quarantine_root", None) if cfg is not None else None
    if isinstance(val, str) and val.strip():
        return val.strip().rstrip("/") or "/quarantine"
    return "/quarantine"


def beets_exec_user_str(cfg: Any = None) -> str:
    val = getattr(cfg, "beets_exec_user", "") if cfg is not None else ""
    return val.strip() if isinstance(val, str) else ""


def beets_job_script(cfg: Any, name: str) -> str:
    return f"{beets_automation_dir_str(cfg)}/jobs/{name.lstrip('/')}"


@dataclass(frozen=True)
class HunterConfig:
    # --- Spotify -----------------------------------------------------------
    spotify_client_id: str
    spotify_client_secret: str
    token_file: Path
    want_sources: tuple[str, ...]        # subset of {"saved_albums", "playlists"}
    playlist_exclude: str                # regex on playlist name; "" = none

    # --- slskd -------------------------------------------------------------
    slskd_url: str
    slskd_api_key: str
    search_timeout: int                  # seconds to wait for a search to complete
    max_grabs_per_pass: int              # avalanche guard
    min_candidate_ratio: float           # dir must have >= expected*this audio files
    max_candidate_ratio: float           # ... and <= expected*this (discography guard)
    prefer_lossless: bool
    min_bitrate: int                     # kbps floor for lossy candidates; 0 = off

    # --- want lifecycle ----------------------------------------------------
    owned_coverage: float                # beets items / spotify tracks >= this = owned
    max_attempts: int                    # real grabs before exhausted
    search_miss_cap: int                 # fruitless searches before exhausted
    peer_ban_strikes: int                # auto-ban a peer after N all-failed grabs
    drop_window_hours: int               # release-day aggressive-hunt window
    backoff_seconds: tuple[int, ...]     # delay before attempt 2, 3, ... (last repeats)
    grab_stale_seconds: int              # grabbed but still not owned after this -> retry

    # --- Prowlarr fallback -------------------------------------------------
    prowlarr_url: str
    prowlarr_api_key: str
    prowlarr_fallback: bool              # UI toggle; needs the key to do anything
    prowlarr_after_misses: int           # consecutive slskd misses before asking

    # --- playlist mirror ---------------------------------------------------
    sync_playlists: bool
    playlist_match_cap: int              # max Subsonic lookups per pass
    playlist_prefix: str                 # prepended to mirrored playlist names
    navidrome_url: str
    navidrome_user: str
    navidrome_pass: str

    # --- notifications -----------------------------------------------------
    webhook_url: str
    notify_min_interval: int

    # --- followed artists ---------------------------------------------------
    artist_check_hours: int              # per-artist new-release check cadence

    # --- web UI ------------------------------------------------------------
    ui_port: int                         # 0 disables the UI entirely
    ui_token: str                        # "" = LAN-trusted (stack default)

    # --- plumbing ----------------------------------------------------------
    apply: bool
    interval: int                        # scheduler cadence (seconds)
    beets_db: Path
    navidrome_db: Path                   # play counts for play-weighted backlog (ro)
    audiomuse_url: str                   # sonic-analysis engine ("" = radio off)
    audiomuse_token: str
    quarantine_dir: Path                 # beets quarantine (ro)
    state_dir: Path
    state_db: Path
    log_dir: Path
    log_keep_days: int
    pass_deadline: int                   # wall-clock budget for one pass

    # --- beets console -----------------------------------------------------
    docker_sock: str                     # /var/run/docker.sock (ro mount)
    beets_container: str                 # container name for exec
    beets_exec_user: str                 # user for docker exec ("" = default)
    beets_automation_dir: Path           # ro mount of beets automation dir
    music_root: Path                     # ro mount of the music library

    # --- Layer 0/1 additions (defaulted for SimpleNamespace/ctor compat) ---
    spotify_proxy: str = ""
    storage_fstype: str | None = None
    trusted_proxies: tuple[str, ...] = _DEFAULT_TRUSTED_PROXIES
    app_name: str = "Hunter"
    user_agent_contact: str = ""
    miss_retry_seconds: int = 259_200
    dl_max_bundle_bytes: int = 30 * (1 << 30)
    dl_keep_bytes: int = 60 * (1 << 30)
    dl_ttl_seconds: int = 24 * 3600
    dl_max_files: int = 20_000
    quarantine_enabled: bool = True
    beets_music_root: str = ""
    beets_quarantine_root: str = ""
    beets_python: str = "python3"
    beets_cli: tuple[str, ...] = ("beet",)
    beets_automation_container_dir: str = "/config/automation"
    hunter_container: str = "hunter"
    slskd_container: str = "slskd"
    navidrome_container: str = "navidrome"
    prowlarr_container: str = "prowlarr"
    services: tuple[str, ...] = ()
    schedule_file: Path | None = None

    # Knobs the web UI may override live via /state/settings.json.
    UI_TUNABLE: ClassVar[dict[str, tuple[int, int] | None]] = {
        "interval": (300, 86_400),
        "max_grabs_per_pass": (1, 50),
        "min_bitrate": (0, 1500),
        "max_attempts": (1, 10),
        "search_miss_cap": (1, 100),
        "prowlarr_after_misses": (1, 20),
        "prefer_lossless": None,          # bool
        "prowlarr_fallback": None,        # bool
    }

    parse_tunable = staticmethod(parse_tunable)

    @classmethod
    def from_env(cls) -> "HunterConfig":
        global _CONFIG_CACHE
        state_dir = Path(_env_str("HUNTER_STATE_DIR", "/state"))
        settings_path = state_dir / "settings.json"
        apply_path = state_dir / "apply_override"
        app_path = state_dir / "spotify_app.json"

        env_sig = tuple(
            sorted(
                (k, v)
                for k, v in os.environ.items()
                if k == "DOCKER_SOCK" or k.startswith(_ENV_PREFIXES)
            )
        )
        q_exists = (
            Path("/quarantine").exists()
            if "HUNTER_QUARANTINE_DIR" not in os.environ
            else None
        )
        cache_key = (
            env_sig,
            q_exists,
            _stat_sig(settings_path),
            _stat_sig(apply_path),
            _stat_sig(app_path),
        )
        with _CACHE_LOCK:
            if _CONFIG_CACHE is not None and _CONFIG_CACHE[0] == cache_key:
                return _CONFIG_CACHE[1]

        cfg = cls._build_from_env(state_dir, settings_path)
        with _CACHE_LOCK:
            _CONFIG_CACHE = (cache_key, cfg)
        return cfg

    @classmethod
    def _build_from_env(cls, state_dir: Path, settings_path: Path) -> "HunterConfig":
        ov = _read_json_dict(settings_path)

        def ovi(key: str, default: int) -> int:
            if key not in ov:
                return default
            try:
                val = parse_tunable(key, ov[key])
            except ValueError:
                return default
            return val if isinstance(val, int) and not isinstance(val, bool) else default

        def ovb(key: str, default: bool) -> bool:
            if key not in ov:
                return default
            try:
                val = parse_tunable(key, ov[key])
            except ValueError:
                return default
            return val if isinstance(val, bool) else default

        # Pair-consistent Spotify app credentials: env pair OR file pair,
        # never mixed across the two sources.
        env_id = _env_str("SPOTIFY_CLIENT_ID", "")
        env_secret = _env_str("SPOTIFY_CLIENT_SECRET", "")
        if env_id and env_secret:
            sp_id, sp_secret = env_id, env_secret
        else:
            if env_id or env_secret:
                log.warning(
                    "Ignoring incomplete SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET env pair"
                )
            sp_id, sp_secret = _read_spotify_app(state_dir)

        raw_sources = [
            s.strip()
            for s in _env_str("HUNTER_WANT_SOURCES", "saved_albums,playlists").split(",")
            if s.strip()
        ]
        unknown_sources = [s for s in raw_sources if s not in _VALID_WANT_SOURCES]
        if unknown_sources:
            log.warning(
                "Unknown HUNTER_WANT_SOURCES entries ignored: %s",
                ", ".join(unknown_sources),
            )
        sources = tuple(s for s in raw_sources if s in _VALID_WANT_SOURCES)

        # Empty or unset HUNTER_STORAGE_FSTYPE disables fstype checking.
        storage_fstype: str | None = (
            os.environ.get("HUNTER_STORAGE_FSTYPE", "").strip() or None
        )

        music_root = Path(_env_str("HUNTER_MUSIC_ROOT", "/music"))
        if "HUNTER_QUARANTINE_DIR" in os.environ:
            q_raw = os.environ["HUNTER_QUARANTINE_DIR"].strip()
            quarantine_enabled = bool(q_raw)
            quarantine_dir = Path(q_raw) if q_raw else Path("/quarantine")
        else:
            quarantine_dir = Path("/quarantine")
            quarantine_enabled = quarantine_dir.exists()

        beets_music_root = (
            _env_str("HUNTER_BEETS_MUSIC_ROOT", str(music_root)).rstrip("/") or "/"
        )
        beets_quarantine_root = (
            _env_str("HUNTER_BEETS_QUARANTINE_ROOT", str(quarantine_dir)).rstrip("/")
            or "/"
        )
        raw_cli = (os.environ.get("BEETS_CLI") or "").strip()
        if raw_cli:
            try:
                beets_cli = tuple(shlex.split(raw_cli)) or ("beet",)
            except ValueError:
                beets_cli = ("beet",)
        else:
            beets_cli = ("beet",)
        raw_sched = _env_str("HUNTER_SCHEDULE_FILE", "")
        schedule_file = Path(raw_sched) if raw_sched else None

        dl_max_bundle_default = int(
            _env_float("HUNTER_DL_MAX_GB", 30.0, 0.1, 500.0) * (1 << 30)
        )
        dl_max_bundle_bytes = _env_int(
            "HUNTER_DL_MAX_BUNDLE_BYTES",
            dl_max_bundle_default,
            1,
            500 * (1 << 30),
        )
        dl_keep_default = int(
            _env_float("HUNTER_DL_KEEP_GB", 60.0, 0.1, 2000.0) * (1 << 30)
        )
        dl_keep_bytes = _env_int(
            "HUNTER_DL_KEEP_BYTES",
            dl_keep_default,
            1,
            2000 * (1 << 30),
        )
        dl_ttl_default = int(
            _env_float("HUNTER_DL_TTL_HOURS", 24.0, 0.1, 720.0) * 3600
        )
        dl_ttl_seconds = _env_int(
            "HUNTER_DL_TTL_SECONDS",
            dl_ttl_default,
            60,
            720 * 3600,
        )

        return cls(
            spotify_client_id=sp_id,
            spotify_client_secret=sp_secret,
            token_file=state_dir / "spotify_token.json",
            want_sources=sources,
            playlist_exclude=_env_str("HUNTER_PLAYLIST_EXCLUDE", ""),
            slskd_url=_env_str("SLSKD_URL", "http://slskd:5030").rstrip("/"),
            slskd_api_key=_env_str("SLSKD_API_KEY", ""),
            search_timeout=_env_int("HUNTER_SEARCH_TIMEOUT", 45, 10, 300),
            max_grabs_per_pass=ovi(
                "max_grabs_per_pass",
                _env_int("HUNTER_MAX_GRABS_PER_PASS", 4, 1, 50),
            ),
            min_candidate_ratio=_env_float("HUNTER_MIN_CANDIDATE_RATIO", 0.8, 0.1, 1.0),
            max_candidate_ratio=_env_float("HUNTER_MAX_CANDIDATE_RATIO", 2.5, 1.0, 10.0),
            prefer_lossless=ovb(
                "prefer_lossless",
                _env_bool("HUNTER_PREFER_LOSSLESS", True),
            ),
            min_bitrate=ovi("min_bitrate", _env_int("HUNTER_MIN_BITRATE", 192, 0, 1500)),
            owned_coverage=_env_float("HUNTER_OWNED_COVERAGE", 0.8, 0.1, 1.0),
            max_attempts=ovi("max_attempts", _env_int("HUNTER_MAX_ATTEMPTS", 3, 1, 10)),
            search_miss_cap=ovi(
                "search_miss_cap",
                _env_int("HUNTER_SEARCH_MISS_CAP", 8, 1, 100),
            ),
            peer_ban_strikes=_env_int("HUNTER_PEER_BAN_STRIKES", 3, 1, 20),
            drop_window_hours=_env_int("HUNTER_DROP_WINDOW_HOURS", 48, 0, 336),
            backoff_seconds=_env_int_tuple(
                "HUNTER_BACKOFF_SECONDS",
                _DEFAULT_BACKOFF,
                minimum=0,
                maximum=30 * 86_400,
            ),
            grab_stale_seconds=_env_int(
                "HUNTER_GRAB_STALE_SECONDS", 172_800, 3600, 2_592_000
            ),
            sync_playlists=_env_bool("HUNTER_SYNC_PLAYLISTS", True),
            playlist_match_cap=_env_int("HUNTER_PLAYLIST_MATCH_CAP", 400, 10, 5000),
            playlist_prefix=_env_str("HUNTER_PLAYLIST_PREFIX", ""),
            navidrome_url=_env_str("NAVIDROME_URL", "http://navidrome:4533").rstrip("/"),
            navidrome_user=_env_str("NAVIDROME_USER", ""),
            navidrome_pass=_env_str("NAVIDROME_PASS", ""),
            webhook_url=_env_str("NOTIFY_WEBHOOK_URL", ""),
            notify_min_interval=_env_int("NOTIFY_MIN_INTERVAL", 1800, 0, 86_400),
            artist_check_hours=_env_int("HUNTER_ARTIST_CHECK_HOURS", 24, 1, 720),
            ui_port=_env_int("HUNTER_UI_PORT", 8080, 0, 65535),
            ui_token=_env_str("HUNTER_UI_TOKEN", ""),
            apply=_resolve_apply(state_dir),
            interval=ovi("interval", _env_int("HUNTER_INTERVAL", 1800, 300, 86_400)),
            beets_db=Path(_env_str("HUNTER_BEETS_DB", "/beets/musiclibrary.db")),
            navidrome_db=Path(_env_str("HUNTER_NAVIDROME_DB", "/navidrome/navidrome.db")),
            audiomuse_url=_env_str("AUDIOMUSE_URL", "http://audiomuse:8000").rstrip("/"),
            audiomuse_token=_env_str("AUDIOMUSE_API_TOKEN", ""),
            prowlarr_url=_env_str("PROWLARR_URL", "http://prowlarr:9696").rstrip("/"),
            prowlarr_api_key=_env_str("PROWLARR_API_KEY", ""),
            prowlarr_fallback=ovb(
                "prowlarr_fallback",
                _env_bool("HUNTER_PROWLARR_FALLBACK", True),
            ),
            prowlarr_after_misses=ovi(
                "prowlarr_after_misses",
                _env_int("HUNTER_PROWLARR_AFTER_MISSES", 2, 1, 20),
            ),
            quarantine_dir=quarantine_dir,
            state_dir=state_dir,
            state_db=state_dir / "hunter.db",
            log_dir=state_dir / "logs",
            log_keep_days=_env_int("HUNTER_LOG_KEEP_DAYS", 30, 1, 365),
            pass_deadline=_env_int("HUNTER_PASS_DEADLINE", 1200, 60, 7200),
            docker_sock=_env_str("DOCKER_SOCK", "/var/run/docker.sock"),
            beets_container=_env_str("BEETS_CONTAINER", "beets"),
            beets_exec_user=(os.environ.get("BEETS_EXEC_USER") or "").strip(),
            beets_automation_dir=Path(
                _env_str("HUNTER_BEETS_AUTOMATION", "/beets-automation")
            ),
            music_root=music_root,
            spotify_proxy=_env_str("SPOTIFY_PROXY", ""),
            storage_fstype=storage_fstype,
            trusted_proxies=_env_csv("HUNTER_TRUSTED_PROXIES", _DEFAULT_TRUSTED_PROXIES),
            app_name=_env_str("HUNTER_APP_NAME", "Hunter"),
            user_agent_contact=_env_str("HUNTER_USER_AGENT_CONTACT", ""),
            miss_retry_seconds=_env_int(
                "HUNTER_MISS_RETRY_SECONDS", 259_200, 60, 30 * 86_400
            ),
            dl_max_bundle_bytes=dl_max_bundle_bytes,
            dl_keep_bytes=dl_keep_bytes,
            dl_ttl_seconds=dl_ttl_seconds,
            dl_max_files=_env_int("HUNTER_DL_MAX_FILES", 20_000, 100, 200_000),
            quarantine_enabled=quarantine_enabled,
            beets_music_root=beets_music_root,
            beets_quarantine_root=beets_quarantine_root,
            beets_python=_env_str("BEETS_PYTHON", "python3"),
            beets_cli=beets_cli,
            beets_automation_container_dir=(
                _env_str(
                    "HUNTER_BEETS_AUTOMATION_CONTAINER_DIR", "/config/automation"
                ).rstrip("/")
                or "/config/automation"
            ),
            hunter_container=_env_str("HUNTER_CONTAINER", "hunter"),
            slskd_container=_env_str("SLSKD_CONTAINER", "slskd"),
            navidrome_container=_env_str("NAVIDROME_CONTAINER", "navidrome"),
            prowlarr_container=_env_str("PROWLARR_CONTAINER", "prowlarr"),
            services=_env_csv("HUNTER_SERVICES", ()),
            schedule_file=schedule_file,
        )

    @property
    def beets_console_available(self) -> bool:
        return os.path.exists(self.docker_sock)

    @property
    def spotify_configured(self) -> bool:
        """True when EITHER auth path is usable: an sp_dc browser cookie
        (no app creds needed), or the app-creds + refresh-token pair."""
        if not self.token_file.exists():
            return False
        state = _read_json_dict(self.token_file)
        if not state:
            return False
        if state.get("sp_dc"):
            return True
        return bool(
            state.get("refresh_token")
            and self.spotify_client_id
            and self.spotify_client_secret
        )

    @property
    def slskd_configured(self) -> bool:
        return bool(self.slskd_url and self.slskd_api_key)

    @property
    def prowlarr_configured(self) -> bool:
        return bool(self.prowlarr_url and self.prowlarr_api_key)


__all__ = [
    "HunterConfig",
    "beets_automation_dir_str",
    "beets_exec_user_str",
    "beets_job_script",
    "beets_python_bin",
    "beets_quarantine_root_str",
    "clear_config_cache",
    "delete_spotify_token",
    "parse_tunable",
    "save_apply_override",
    "save_spotify_app",
    "save_spotify_token",
    "save_tunables",
]

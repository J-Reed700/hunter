# Hunter v5.0.0

Hunter is a lightweight, self-hosted music acquisition orchestrator and library console. It connects your MusicBrainz requests, Spotify library, Soulseek (`slskd`), Prowlarr, beets library, and Navidrome playlists into a single container with a minimal dependency footprint (`python:3.12-alpine` + `requests`).

```
                    ┌──────────────────────── hunter container ─────────────────────────┐
                    │                                                                   │
 Spotify API ──────▶│  SYNC        Saved albums + playlists ──▶ wants DB (/state)       │
                    │   │                                                               │
 beets DB ─────────▶│  RECONCILE   Wants ⇄ beets DB (read-only owned oracle)            │
 (mode=ro)          │   │          Owned = normalized artist/album + coverage threshold │
                    │   │                                                               │
 slskd / Prowlarr ◀▶│  HUNT        Tiered search ──▶ rank (lossless → fit → slot)       │
                    │   │          ──▶ enqueue best candidate (shadow or live mode)     │
                    │   │                                                               │
 Navidrome API ◀───│  MIRROR      Cached Spotify playlists ──▶ Navidrome (Subsonic)    │
                    └───────────────────────────────────────────────────────────────────┘
```

Hunter never deletes or modifies your media files directly. It enqueues downloads and syncs playlists; optional library mutations (tagging, removal to quarantine, blocklist updates) execute inside your beets container through allowlisted, dry-run-first jobs.

---

## Quick Start

1. **Copy the example configuration files:**
   ```sh
   cp docker-compose.example.yml docker-compose.yml
   cp .env.example .env
   ```

2. **Edit `.env` and `docker-compose.yml`:**
   - Mount your persistent state directory to `/state` and your music library (read-only) to `/music`.
   - Set `SLSKD_URL` and `SLSKD_API_KEY` (and optionally `NAVIDROME_*`, `PROWLARR_*`, `SPOTIFY_*`, `NOTIFY_WEBHOOK_URL`).
   - Keep `HUNTER_APPLY=0` initially to run in **Shadow Mode** (syncs, reconciles, and searches without enqueueing downloads or writing playlists).

3. **Start Hunter:**
   ```sh
   docker compose up -d --build
   ```

4. **Open the Web UI** at `http://<host>:8080`.
   - On first visit, Hunter starts in **setup mode** and prompts you to create the initial `admin` account.
   - Connect Spotify from **Settings → Spotify** (either via `sp_dc` browser cookie or OAuth developer app) if you want playlist/album sync.
   - Flip the **SHADOW ⇄ LIVE** switch in the top bar (or set `HUNTER_APPLY=1` in `.env`) when you are ready to enqueue real downloads.

---

## Architecture Overview

Hunter is organized into strict, acyclic layers under `hunter/`:

1. **Layer 0–1 — Foundation (`domain.py`, `config.py`, `storage.py`, `lock.py`, `db.py`, `http_client.py`, `dockerctl.py`, `notify.py`):**
   Pure normalization and path-confinement helpers, env/override configuration (`HunterConfig`), mount and filesystem health probes, `flock` concurrency locks, SQLite schema/WAL helpers, bounded-timeout HTTP sessions, Docker socket client, and rate-limited webhook alerts.
2. **Layer 2 — Data & Auth (`state.py`, `auth.py`, `library.py`, `blocklist.py`, `libquery.py`):**
   `hunter.db` wants/events/audit store, scrypt password hashing + cookie/token RBAC, read-only beets library queries (`mode=ro`), faceted album/artist/track filtering, duplicate detection, and import blocklist reader.
3. **Layer 3 — External Clients (`slskd_client.py`, `prowlarr.py`, `spotify.py`, `spgql.py`, `spsync.py`, `playlist_sync.py`, `mb.py`, `listenbrainz.py`):**
   Integrations for Soulseek (`slskd`), Prowlarr fallback search, resumable Spotify sync (OAuth Web API + cookie GraphQL), Subsonic playlist mirroring, MusicBrainz metadata/cover art, and ListenBrainz popularity data.
4. **Layer 4 — Operations & Pipelines (`pipeline.py`, `beetcmd.py`, `beetsops.py`, `jobs.py`, `libops.py`, `downloads.py`, `recovery.py`):**
   Orchestration pass (`sync` → `reconcile` → `hunt` → `mirror`), allowlisted `beet` command builder with blast-radius previews, optional beets automation script runner, background job tracker, journaled undoable library removals, streaming ZIP bundle builder, and quarantine recovery.
5. **Layer 5 — Entrypoints (`cli.py`, `scheduler.py`, `web/`):**
   Supervisor PID 1 (`scheduler.py`) spawning bounded `hunter pass` subprocesses, the `hunter` operator CLI, and the threaded stdlib HTTP server + zero-build ES-module SPA (`hunter/web/static/`).

### Key Design Guarantees
- **beets DB is the single owned-oracle:** Opened strictly read-only (`mode=ro`) to avoid dual-truth re-grab loops.
- **Shadow mode by default (`HUNTER_APPLY=0`):** Toggleable live from the Web UI via `/state/apply_override` without restarting the container.
- **Bounded HTTP timeouts:** External client requests enforce explicit timeouts and retry/circuit-breaker guards.
- **Journaled, undoable library removals:** Removing artists/albums/tracks from the Web UI moves files to quarantine with a JSON manifest so removals can be undone from **Library → Removed**.

---

## Environment Variables

See [`.env.example`](.env.example) for the complete annotated list. Key variables include:

### Core & Storage
| Variable | Default | Description |
|---|---|---|
| `HUNTER_APPLY` | `0` | `0` = shadow mode, `1` = live mode (overridden by UI toggle `/state/apply_override`) |
| `HUNTER_INTERVAL` | `1800` | Seconds between scheduled passes (`300`–`86400`) |
| `HUNTER_PASS_DEADLINE` | `1200` | Wall-clock timeout in seconds for a single pass |
| `HUNTER_STATE_DIR` | `/state` | Persistent directory for `hunter.db`, logs, art cache, tokens, and backups |
| `HUNTER_MUSIC_ROOT` | `/music` | Music library root mounted inside Hunter (read-only) |
| `HUNTER_BEETS_DB` | `/beets/musiclibrary.db` | Path to beets SQLite database (opened `mode=ro`) |
| `HUNTER_NAVIDROME_DB` | `/navidrome/navidrome.db` | Optional Navidrome SQLite DB (read-only) for play-weighted backlog sorting |
| `HUNTER_QUARANTINE_DIR` | `/quarantine` | Quarantine root; disabled automatically if unset and `/quarantine` does not exist |
| `HUNTER_STORAGE_FSTYPE` | *(empty)* | Optional required filesystem type (e.g. `fuse.mergerfs`). Leave empty for standard filesystems (`ext4`, `xfs`, `zfs`, `btrfs`, `apfs`, etc.) |
| `HUNTER_APP_NAME` | `Hunter` | Application prefix used in outbound HTTP `User-Agent` headers |
| `HUNTER_USER_AGENT_CONTACT` | *(empty)* | Optional contact email or URL appended to MusicBrainz/CoverArtArchive `User-Agent` |

### Integrations & Hunting
| Variable | Default | Description |
|---|---|---|
| `SLSKD_URL` / `SLSKD_API_KEY` | `http://slskd:5030` / *(empty)* | `slskd` REST endpoint and API key |
| `NAVIDROME_URL` / `NAVIDROME_USER` / `NAVIDROME_PASS` | `http://navidrome:4533` / *(empty)* | Navidrome server and credentials for Subsonic playlist mirroring |
| `PROWLARR_URL` / `PROWLARR_API_KEY` | `http://prowlarr:9696` / *(empty)* | Optional Prowlarr fallback after `HUNTER_PROWLARR_AFTER_MISSES` Soulseek misses |
| `AUDIOMUSE_URL` / `AUDIOMUSE_API_TOKEN` | `http://audiomuse:8000` / *(empty)* | Optional AudioMuse endpoint for sonic radio playlists |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | *(empty)* | Optional Spotify developer app credentials (can also be saved in the Web UI) |
| `SPOTIFY_PROXY` | *(empty)* | Optional HTTP/SOCKS proxy URL for outbound Spotify requests |
| `NOTIFY_WEBHOOK_URL` | *(empty)* | Optional webhook URL (ntfy-compatible) for grab/exhaustion/health alerts |
| `HUNTER_WANT_SOURCES` | `saved_albums,playlists` | Comma-separated Spotify auto-want sources (`saved_albums`, `playlists`) |
| `HUNTER_OWNED_COVERAGE` | `0.8` | Minimum track coverage ratio (`0.1`–`1.0`) to treat an album as owned |
| `HUNTER_PREFER_LOSSLESS` | `1` | Prefer lossless candidates over lossy |
| `HUNTER_MIN_BITRATE` | `192` | Minimum kbps for lossy candidates (`0` disables) |

### Web UI, Reverse Proxy & Beets Console
| Variable | Default | Description |
|---|---|---|
| `HUNTER_UI_PORT` | `8080` | Web UI listen port (`0` disables the HTTP server) |
| `HUNTER_UI_TOKEN` | *(empty)* | Optional static admin API token (`X-Hunter-Token` header) for scripts/automation |
| `HUNTER_TRUSTED_PROXIES` | `127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | Trusted reverse-proxy CIDRs for `X-Forwarded-For`, `X-Forwarded-Proto`, and `X-Forwarded-Host` |
| `DOCKER_SOCK` | `/var/run/docker.sock` | Docker socket path (mount `:ro` to enable container health checks and Beets console) |
| `HUNTER_SERVICES` | *(empty)* | Comma-separated container names to monitor on Dashboard/Settings; empty auto-discovers core containers (`hunter`, `beets`, `slskd`, `navidrome`, `prowlarr`) |
| `BEETS_CONTAINER` | `beets` | Target beets container name for `docker exec` |
| `BEETS_EXEC_USER` | *(empty)* | User for `docker exec` inside the beets container (empty uses the container's default user) |
| `BEETS_PYTHON` | `python3` | Python interpreter inside the beets container |
| `BEETS_CLI` | `beet` | `beet` command inside the beets container (supports space-separated args) |
| `HUNTER_BEETS_MUSIC_ROOT` | `HUNTER_MUSIC_ROOT` | Music root path inside the beets container (if mapped differently from Hunter) |
| `HUNTER_BEETS_QUARANTINE_ROOT` | `HUNTER_QUARANTINE_DIR` | Quarantine root path inside the beets container |
| `HUNTER_BEETS_AUTOMATION` | `/beets-automation` | Optional automation scripts & blocklist mount inside Hunter |
| `HUNTER_BEETS_AUTOMATION_CONTAINER_DIR` | `/config/automation` | Matching automation directory path inside the beets container |
| `HUNTER_SCHEDULE_FILE` | *(empty)* | Optional JSON file of scheduled jobs (`[{"time": "...", "job": "...", "what": "..."}]`); defaults to `<HUNTER_BEETS_AUTOMATION>/schedule.json` |

---

## Reverse-Proxy Deployment

Hunter's built-in HTTP server supports running behind any reverse proxy (Nginx, Caddy, Traefik, Apache, etc.):

- **Forwarded headers:** Hunter evaluates `X-Forwarded-For` right-to-left and only trusts `X-Forwarded-Proto` / `X-Forwarded-Host` when the TCP peer is inside `HUNTER_TRUSTED_PROXIES`. If your proxy runs on a non-RFC1918 IP or custom bridge subnet, add its CIDR to `HUNTER_TRUSTED_PROXIES`.
- **HTTPS & cookies:** When `X-Forwarded-Proto: https` is present from a trusted proxy, Hunter sets `Secure` on session cookies and emits `Strict-Transport-Security`.
- **CSRF protection:** State-changing `POST` requests authenticated via cookies verify `Sec-Fetch-Site` and `Origin`/`Referer` against the effective `Host` (including trusted `X-Forwarded-Host`). Ensure your reverse proxy preserves the public `Host` or forwards `X-Forwarded-Host`.
- **Streaming & timeouts:** ZIP bundle downloads (`/api/dl/...`) support HTTP `Range` requests and can be large; disable proxy response buffering if needed and allow generous read timeouts for `/api/candidates` (up to ~45s).

---

## CLI Commands

Run commands inside the container via `docker exec hunter hunter <command>`:

```sh
hunter status                                   # Print want counts and open wants
hunter pass [--apply|--dry-run] [--limit N]     # Run one orchestration pass
hunter add "Artist" "Album" [--force]           # Add a manual want (checks owned-gate)
hunter drop "Artist" ["Album"] [--source SRC]   # Un-want matching SQL LIKE patterns
hunter seed-gaps [--dry-run]                    # Seed wants for partial albums in beets
hunter seed-upgrades [--dry-run]                # Seed lossless-upgrade wants for lossy albums
hunter auth                                     # Interactive Spotify OAuth token setup
hunter health                                   # JSON healthcheck output (used by Compose)
```

### User Management (`hunter user`)
```sh
hunter user list
hunter user add <username> [--role viewer|operator|admin]
hunter user passwd <username>
hunter user role <username> <viewer|operator|admin>
hunter user enable|disable|delete|revoke <username>
```

---

## Web UI Roles (RBAC)

Hunter enforces role-based access control across all Web UI views and API routes:

| Role | Permissions |
|---|---|
| **`viewer`** | Read-only access to Dashboard, Discover, Queue, Artists, Spotify, Library, Downloads, Activity, and own Account/session management. Can download single tracks and ZIP bundles. |
| **`operator`** | All `viewer` permissions plus creating/editing/dropping wants, running passes, toggling Shadow/Live mode, managing peers and blocklists, running allowlisted Beets console commands/scripts, and performing journaled library removals/undos. |
| **`admin`** | All `operator` permissions plus user management (`Settings → Users`), viewing the audit trail (`Activity → Audit`), and full API token equivalence (`X-Hunter-Token` operates as a synthetic admin). |

> **Password recovery:** If all admin passwords are lost, reset one from the host shell with `docker exec -it hunter hunter user passwd <username>`.

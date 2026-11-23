"""hunter — automated music acquisition and library orchestrator.

One container, four responsibilities, replacing Lidarr + soularr ×3:

    SYNC       Spotify saved albums + playlists  →  wants DB
    RECONCILE  wants                             ⇄  beets DB (owned-oracle)
    HUNT       slskd / Prowlarr search → ranked candidates → enqueue best
    MIRROR     Spotify playlists → Navidrome playlists (Subsonic)

Layered architecture:
    Layer 0  domain.py, config.py, storage.py, lock.py
    Layer 1  db.py, http_client.py, dockerctl.py, notify.py
    Layer 2  state.py, auth.py, library.py, blocklist.py, libquery.py
    Layer 3  slskd_client.py, prowlarr.py, spotify.py, spgql.py, spsync.py,
             playlist_sync.py, mb.py, listenbrainz.py
    Layer 4  pipeline.py, beetcmd.py, beetsops.py, jobs.py, libops.py,
             downloads.py, recovery.py
    Layer 5  cli.py, scheduler.py, web/
"""

__version__ = "5.0.0"

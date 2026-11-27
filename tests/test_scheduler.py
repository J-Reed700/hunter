"""Unit tests for hunter.scheduler."""
from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from hunter import scheduler
from hunter.config import HunterConfig, clear_config_cache


def _make_cfg(tmp: Path) -> HunterConfig:
    return HunterConfig(
        spotify_client_id="",
        spotify_client_secret="",
        token_file=tmp / "tok.json",
        want_sources=("saved_albums",),
        playlist_exclude="",
        slskd_url="",
        slskd_api_key="",
        search_timeout=15,
        max_grabs_per_pass=4,
        min_candidate_ratio=0.8,
        max_candidate_ratio=2.5,
        prefer_lossless=True,
        min_bitrate=192,
        owned_coverage=0.8,
        max_attempts=3,
        search_miss_cap=8,
        peer_ban_strikes=3,
        drop_window_hours=48,
        backoff_seconds=(60,),
        grab_stale_seconds=3600,
        prowlarr_url="",
        prowlarr_api_key="",
        prowlarr_fallback=False,
        prowlarr_after_misses=2,
        sync_playlists=False,
        playlist_match_cap=100,
        playlist_prefix="",
        navidrome_url="",
        navidrome_user="",
        navidrome_pass="",
        webhook_url="http://ntfy/test",
        notify_min_interval=1800,
        artist_check_hours=24,
        ui_port=0,
        ui_token="",
        apply=False,
        interval=1,
        beets_db=tmp / "musiclibrary.db",
        navidrome_db=tmp / "nd.db",
        audiomuse_url="",
        audiomuse_token="",
        quarantine_dir=tmp / "q",
        state_dir=tmp / "nested_state",
        state_db=tmp / "nested_state" / "hunter.db",
        log_dir=tmp / "logs",
        log_keep_days=7,
        pass_deadline=60,
        docker_sock="",
        beets_container="beets",
        beets_exec_user="abc",
        beets_automation_dir=tmp / "auto",
        music_root=tmp / "merged",
    )


class TestScheduler(unittest.TestCase):
    def tearDown(self) -> None:
        scheduler._stop_event.clear()
        clear_config_cache()

    def test_signal_handler_only_sets_event(self) -> None:
        scheduler._stop_event.clear()
        with patch("hunter.scheduler.log") as mock_log:
            scheduler._handle_signal(signal.SIGTERM, None)
            self.assertTrue(scheduler._stop_event.is_set())
            mock_log.info.assert_not_called()

    def test_wait_for_pass_shutdown_and_timeout(self) -> None:
        # Shutdown terminates child cleanly
        proc = MagicMock(spec=subprocess.Popen)
        proc.poll.side_effect = [None, None, 0]
        scheduler._stop_event.set()
        rc = scheduler._wait_for_pass(proc, 60.0)
        self.assertEqual(rc, 0)
        proc.terminate.assert_called_once()

        # Timeout sends SIGTERM then SIGKILL if child refuses to exit
        scheduler._stop_event.clear()
        proc2 = MagicMock(spec=subprocess.Popen)
        proc2.poll.return_value = None
        proc2.wait.return_value = -9
        clock = {"t": 0.0}

        def advance(dt: float) -> None:
            clock["t"] += dt

        with patch("hunter.scheduler.time.monotonic", side_effect=lambda: clock["t"]), \
             patch("hunter.scheduler.time.sleep", side_effect=advance):
            rc2 = scheduler._wait_for_pass(proc2, 0.0)
        self.assertEqual(rc2, -9)
        proc2.terminate.assert_called_once()
        proc2.kill.assert_called_once()

    def test_reap_zombies_preserves_tracked_local_job_exit_codes(self) -> None:
        from types import SimpleNamespace
        from hunter import jobs

        # Case 1: exited PID is tracked in jobs.tracked_local_pids() -> waitpid NOT called!
        tracked_res = SimpleNamespace(si_pid=4242)
        with patch.object(jobs, "tracked_local_pids", return_value={4242}, create=True), \
             patch("hunter.scheduler.os.waitid", return_value=tracked_res) as mock_waitid, \
             patch("hunter.scheduler.os.waitpid") as mock_waitpid:
            scheduler._reap_zombies()
        mock_waitid.assert_called_once()
        mock_waitpid.assert_not_called()

        # Case 2: exited PID is NOT tracked -> waitpid reaps it cleanly
        orphan_res = SimpleNamespace(si_pid=9999)
        with patch.object(jobs, "tracked_local_pids", return_value={4242}, create=True), \
             patch("hunter.scheduler.os.waitid", side_effect=[orphan_res, None]), \
             patch("hunter.scheduler.os.waitpid", return_value=(9999, 0)) as mock_waitpid:
            scheduler._reap_zombies()
        mock_waitpid.assert_called_once_with(9999, scheduler.os.WNOHANG)

    def test_run_forever_lifecycle_alerts_and_atomic_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cfg = _make_cfg(Path(d))
            http_srv = MagicMock()
            fake_jobs = MagicMock()

            cycles = {"n": 0}

            def fake_wait(proc, budget):
                cycles["n"] += 1
                if cycles["n"] >= 4:
                    scheduler._stop_event.set()
                # 3 failed passes, 4th lock-busy (rc=3) -> failure alert fires ONCE at 3, NOT on rc=3!
                return 3 if cycles["n"] == 4 else 1

            with patch("hunter.scheduler.HunterConfig.from_env", return_value=cfg), \
                 patch("hunter.downloads.recover_on_startup") as mock_rec, \
                 patch("hunter.web.server.start_ui", return_value=http_srv), \
                 patch("hunter.jobs.Jobs", return_value=fake_jobs), \
                 patch("hunter.scheduler.subprocess.Popen", return_value=MagicMock()), \
                 patch("hunter.scheduler._wait_for_pass", side_effect=fake_wait), \
                 patch("hunter.scheduler.Notifier.send") as mock_send, \
                 patch("hunter.scheduler._reap_zombies") as mock_reap:
                rc = scheduler.run_forever()

            self.assertEqual(rc, 0)
            mock_rec.assert_called_once_with(cfg)
            http_srv.shutdown.assert_called_once()
            http_srv.server_close.assert_called_once()
            self.assertGreaterEqual(fake_jobs.refresh.call_count, 4)
            self.assertGreaterEqual(mock_reap.call_count, 4)
            # Only 1 failure alert (on cycle 3, NOT re-fired on cycle 4 when rc==3)
            mock_send.assert_called_once()
            self.assertTrue(mock_send.call_args.kwargs["force"])
            self.assertEqual(mock_send.call_args.kwargs["bucket"], "alert")

            sup = json.loads((cfg.state_dir / "supervisor.json").read_text(encoding="utf-8"))
            self.assertEqual(sup["rc"], 3)
            self.assertEqual(sup["consecutive_failures"], 3)
            self.assertEqual(sup["lock_busy_streak"], 1)


if __name__ == "__main__":
    unittest.main()

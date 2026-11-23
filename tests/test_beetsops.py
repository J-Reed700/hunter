from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import beetsops


class BeetsOpsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auto = self.root / "beets-auto"
        (self.auto / "logs").mkdir(parents=True)
        (self.auto / "state").mkdir(parents=True)
        self.merged = self.root / "merged"
        self.merged.mkdir()
        self.quarantine = self.root / "quarantine"
        (self.quarantine / "music" / "2026-11-21" / "unverified").mkdir(parents=True)
        self.cfg = SimpleNamespace(
            beets_automation_dir=self.auto,
            beets_container="beets",
            beets_exec_user="abc",
            beets_db=self.root / "beets" / "musiclibrary.db",
            music_root=self.merged,
            quarantine_dir=self.quarantine,
            log_dir=self.root / "logs",
            docker_sock=str(self.root / "docker.sock"),
        )
        beetsops.cache._d.clear()

    def test_start_script_rejects_internal_and_disallowed_extra_args(self) -> None:
        jobs = mock.MagicMock()
        r_int = beetsops.start_script(jobs, "resilient_import")
        self.assertFalse(r_int["ok"])
        self.assertIn("internal", r_int["error"])

        r_smuggle = beetsops.start_script(
            jobs, "sweep_empty_dirs", apply=False, extra=["--apply"]
        )
        self.assertFalse(r_smuggle["ok"])
        self.assertIn("not allowed", r_smuggle["error"])
        jobs.start_beets.assert_not_called()

    def test_watcher_status_runs_as_exec_user_and_no_double_count(self) -> None:
        log_file = self.auto / "logs" / "import_watcher.log"
        log_file.write_text(
            "2026-11-21 17:07:03,662 INFO  lib.resilient_importer: [slskd] blocklist: routed 5 files to /quarantine/blocked_artist\n"
            "2026-11-21 17:07:03,662 INFO  watcher: [slskd] cascade: 5 initial → 5 imported across ['blocklist'], 0 quarantined, 0 remaining\n"
            "2026-11-21 18:00:00,000 INFO  watcher: [slskd] cascade: 3 initial → 2 imported across ['mb_autotag'], 1 quarantined, 0 remaining\n",
            encoding="utf-8",
        )
        docker = mock.MagicMock()
        docker.exec_run.return_value = {"exit_code": 0, "output": "123\n"}
        docker.state.return_value = {"running": True}

        st = beetsops.watcher_status(self.cfg, docker, ttl=0)
        self.assertTrue(st["alive"])
        self.assertEqual(st["counts"]["quarantined"], 6)
        self.assertEqual(st["counts"]["imported"], 2)
        docker.exec_run.assert_called_once_with(
            "beets", ["pgrep", "-f", "python.*import_watcher"], user="abc", timeout=20
        )

        beetsops.restart_watcher(self.cfg, docker)
        docker.exec_run.assert_called_with(
            "beets", ["pkill", "-f", "python.*import_watcher"], user="abc", timeout=20
        )

    def test_cache_single_flight_never_caches_failures_and_source_dirs_key(self) -> None:
        c = beetsops._Cache()
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            return {"ok": False, "error": "down"}

        self.assertFalse(c.get("k", 60, fail_once)["ok"])
        self.assertFalse(c.get("k", 60, fail_once)["ok"])
        self.assertEqual(calls, 2)  # never cached ok=False

        # Single-flight concurrency
        slow_calls = 0

        def slow_ok():
            nonlocal slow_calls
            slow_calls += 1
            time.sleep(0.05)
            return {"ok": True, "n": slow_calls}

        results: list[dict] = []
        threads = [
            threading.Thread(target=lambda: results.append(c.get("sf", 60, slow_ok)))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(slow_calls, 1)
        self.assertEqual(len(results), 5)

        # source_dirs caches separately per per_source
        dl = self.merged / "Downloads"
        for i in range(5):
            (dl / f"alb{i}").mkdir(parents=True)
        docker = mock.MagicMock()
        docker.env_of.return_value = {"BEETS_SOURCES": "slskd:album_per_dir:/merged/Downloads"}
        r1 = beetsops.source_dirs(self.cfg, docker, ttl=60, per_source=2)
        r2 = beetsops.source_dirs(self.cfg, docker, ttl=60, per_source=4)
        self.assertEqual(len(r1["sources"][0]["dirs"]), 2)
        self.assertEqual(len(r2["sources"][0]["dirs"]), 4)
        beetsops.cache.drop("source_dirs")
        self.assertNotIn("source_dirs:2", beetsops.cache._d)

    def test_quarantine_symlink_containment_and_walk_cap(self) -> None:
        outside = self.root / "secret.txt"
        outside.write_text("secret", encoding="utf-8")
        qdir = self.quarantine / "music" / "2026-11-21" / "unverified" / "bad_album"
        qdir.mkdir(parents=True)
        os.symlink(outside, qdir / "escape.txt")
        for i in range(450):
            (qdir / f"track_{i:03d}.flac").write_bytes(b"x")

        entry = beetsops.quarantine_entry(self.cfg, "2026-11-21", "unverified", "bad_album")
        self.assertIsNotNone(entry)
        self.assertEqual(len(entry["files"]), 400)
        self.assertNotIn("escape.txt", {f["name"] for f in entry["files"]})

        # Symlinked directory escaping quarantine root must return None
        escape_dir = self.root / "outside_dir"
        escape_dir.mkdir()
        os.symlink(escape_dir, self.quarantine / "music" / "2026-11-21" / "unverified" / "sym_album")
        self.assertIsNone(
            beetsops.quarantine_entry(self.cfg, "2026-11-21", "unverified", "sym_album")
        )

    def test_script_catalogue_filters_by_existing_files_and_load_schedule(self) -> None:
        jobs = mock.MagicMock()
        jobs.list.return_value = []
        # No jobs dir -> empty catalogue
        self.assertEqual(beetsops.script_catalogue(jobs, self.cfg)["scripts"], [])

        jobs_dir = self.auto / "jobs"
        jobs_dir.mkdir(parents=True)
        (jobs_dir / "maintenance.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (jobs_dir / "sweep_empty_album_dirs.py").write_text("print('ok')\n", encoding="utf-8")
        ids = [s["id"] for s in beetsops.script_catalogue(jobs, self.cfg)["scripts"]]
        self.assertEqual(ids, ["maintenance", "sweep_empty_dirs"])

        # Schedule loading: absent -> [], invalid JSON -> [], valid list -> parsed
        self.assertEqual(beetsops.load_schedule(self.cfg), [])
        sched_path = self.auto / "schedule.json"
        sched_path.write_text("{bad json", encoding="utf-8")
        self.assertEqual(beetsops.load_schedule(self.cfg), [])
        sched_path.write_text(
            '[{"time": "03:00 daily", "job": "maintenance.sh", "what": "nightly"}]',
            encoding="utf-8",
        )
        self.assertEqual(
            beetsops.load_schedule(self.cfg),
            [{"time": "03:00 daily", "job": "maintenance.sh", "what": "nightly"}],
        )

    def test_configured_containers_and_python_used_in_ops(self) -> None:
        self.cfg.beets_python = "/venv/bin/python3"
        self.cfg.beets_automation_container_dir = "/srv/auto"
        self.cfg.hunter_container = "my-hunter"
        self.cfg.slskd_container = "my-slskd"
        self.cfg.navidrome_container = "my-navi"

        sources = beetsops.log_sources(self.cfg)
        self.assertEqual(sources["hunter-container"], ("docker", "my-hunter"))
        self.assertEqual(sources["slskd-container"], ("docker", "my-slskd"))
        self.assertEqual(sources["navidrome-container"], ("docker", "my-navi"))

        docker = mock.MagicMock()
        docker.exec_run.return_value = {"exit_code": 0, "output": "ok"}
        res = beetsops.trigger_rescan(self.cfg, docker, app="navidrome")
        self.assertTrue(res["ok"])
        docker.exec_run.assert_called_once_with(
            "beets",
            ["/venv/bin/python3", "/srv/auto/services/trigger_rescan.py", "--app", "navidrome"],
            user="abc",
            timeout=120,
        )


if __name__ == "__main__":
    unittest.main()

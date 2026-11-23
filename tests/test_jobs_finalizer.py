import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import jobs as jobs_mod
from hunter.jobs import Jobs


def _cfg(d: str):
    return SimpleNamespace(state_db=Path(d) / "hunter.db", log_dir=Path(d) / "logs",
                           beets_automation_dir=Path(d) / "auto", docker_sock=f"{d}/no.sock",
                           beets_container="beets", beets_exec_user="abc")


class FinalizerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.calls = []
        jobs_mod.register_finalizer("library", lambda j, job: self.calls.append(job["id"]))

    def tearDown(self):
        jobs_mod._finalizers.pop("library", None)
        self.tmp.cleanup()

    def _insert_finished_local_job(self, job_id="j1", rc=3):
        log = Path(self.tmp.name) / f"{job_id}.log"
        log.write_text(f"# hunter job {job_id}\nRESULT {{\"ok\": false}}\n# finished\nexit={rc}\n")
        Jobs(self.cfg)._insert({"id": job_id, "kind": "library", "title": "t", "argv": ["x"],
                                "mode": "apply", "runner": "local", "pid": None, "log": str(log),
                                "started_ts": 1, "meta": {"op": "remove"}, "origin": "ui"})
        return job_id

    def test_refresh_settles_and_finalizes_exactly_once(self):
        jid = self._insert_finished_local_job()
        Jobs(self.cfg).refresh()
        Jobs(self.cfg).refresh()
        job = Jobs(self.cfg).get(jid)
        self.assertEqual(self.calls, [jid])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["exit_code"], 3)
        self.assertIn("finalized_ts", job["meta"])

    def test_get_also_settles_and_finalizes_once(self):
        jid = self._insert_finished_local_job("j2", rc=0)
        job = Jobs(self.cfg).get(jid)
        self.assertEqual(job["status"], "ok")
        Jobs(self.cfg).get(jid)
        Jobs(self.cfg).refresh()
        self.assertEqual(self.calls, [jid])

    def test_finalizer_exception_is_contained(self):
        def boom(j, job):
            raise RuntimeError("nope")
        jobs_mod.register_finalizer("library", boom)
        jid = self._insert_finished_local_job("j3")
        Jobs(self.cfg).refresh()
        self.assertIn("finalized_ts", Jobs(self.cfg).get(jid)["meta"])

    def test_explicit_finalizers_injection_and_missing_finalizer_deferred(self):
        jobs_mod._finalizers.pop("library", None)
        jid = self._insert_finished_local_job("j_defer")
        # Without a registered/injected finalizer for 'library', finalized_ts must NOT be stamped
        Jobs(self.cfg).refresh()
        self.assertNotIn("finalized_ts", Jobs(self.cfg).get(jid)["meta"])
        # Injecting finalizers explicitly via Jobs(..., finalizers=...) finalizes it once
        seen: list[str] = []
        Jobs(self.cfg, finalizers={"library": lambda _j, job: seen.append(job["id"])}).refresh()
        self.assertEqual(seen, [jid])
        self.assertIn("finalized_ts", Jobs(self.cfg).get(jid)["meta"])

    def test_beets_probe_checks_exec_inspect_first_even_with_exit_in_log(self):
        log_dir = self.cfg.beets_automation_dir / "logs" / "jobs"
        log_dir.mkdir(parents=True)
        (log_dir / "j_beets.log").write_text("# running\nexit=0\n")
        j = Jobs(self.cfg)
        j._insert({"id": "j_beets", "kind": "beet", "title": "t", "argv": ["beet", "ls"],
                   "mode": "run", "runner": "beets", "exec_id": "ex1",
                   "log": str(log_dir / "j_beets.log"), "started_ts": int(time.time()),
                   "meta": {}, "origin": "ui"})
        with mock.patch.object(j.docker, "exec_inspect", return_value={"running": True}):
            got = j.get("j_beets")
            self.assertEqual(got["status"], "running")
        with mock.patch.object(j.docker, "exec_inspect", return_value={"running": False, "exit_code": 0}):
            got2 = j.get("j_beets")
            self.assertEqual(got2["status"], "ok")

    def test_start_lock_serializes_exclusive_beets_jobs(self):
        sock = Path(self.cfg.docker_sock)
        sock.write_text("")
        j = Jobs(self.cfg)
        results: list[dict] = []
        barrier = threading.Barrier(4)

        def fake_exec(*a, **kw):
            time.sleep(0.02)
            return {"exec_id": f"e-{threading.get_ident()}"}

        def launch():
            barrier.wait()
            res = j.start_beets(kind="script", title="maint", argv=["/bin/true"],
                                exclusive="maintenance")
            results.append(res)

        with mock.patch.object(j.docker, "exec_run", side_effect=fake_exec), \
             mock.patch.object(j.docker, "exec_inspect", return_value={"running": True}):
            threads = [threading.Thread(target=launch) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        ok_count = sum(1 for r in results if r["ok"])
        self.assertEqual(ok_count, 1)

    def test_prune_logs_ignores_ro_beets_logs_and_cancel_checks_kill(self):
        ro_dir = self.cfg.beets_automation_dir / "logs" / "jobs"
        ro_dir.mkdir(parents=True)
        ro_log = ro_dir / "old.log"
        ro_log.write_text("keep")
        local_dir = self.cfg.log_dir / "jobs"
        local_dir.mkdir(parents=True)
        local_old = local_dir / "old_local.log"
        local_old.write_text("drop")
        import os
        old_ts = time.time() - 30 * 86_400
        os.utime(ro_log, (old_ts, old_ts))
        os.utime(local_old, (old_ts, old_ts))

        j = Jobs(self.cfg)
        self.assertEqual(j.prune_logs(), 1)
        self.assertTrue(ro_log.exists())
        self.assertFalse(local_old.exists())

        j._insert({"id": "jb2", "kind": "beet", "title": "t", "argv": ["x"],
                   "mode": "run", "runner": "beets", "exec_id": "e2",
                   "log": str(ro_log), "started_ts": int(time.time()), "meta": {}, "origin": "ui"})
        with mock.patch.object(j.docker, "exec_inspect", return_value={"running": True}), \
             mock.patch.object(j.docker, "exec_run", return_value={"exit_code": 1, "output": "no-pid"}):
            c_res = j.cancel("jb2")
            self.assertFalse(c_res["ok"])

    def test_get_and_list_refresh_false_skips_exec_inspect(self):
        j = Jobs(self.cfg)
        j._insert({"id": "jb3", "kind": "beet", "title": "t", "argv": ["x"],
                   "mode": "run", "runner": "beets", "exec_id": "e3",
                   "log": str(self.cfg.beets_automation_dir / "x.log"),
                   "started_ts": int(time.time()), "meta": {}, "origin": "ui"})
        with mock.patch.object(j.docker, "exec_inspect") as insp:
            got = j.get("jb3", refresh=False)
            self.assertEqual(got["status"], "running")
            rows = j.list(refresh=False)
            self.assertEqual(len(rows), 1)
            insp.assert_not_called()

    def test_tracked_local_pids_and_begin_immediate_reservation(self) -> None:
        import sys
        j = Jobs(self.cfg)
        res = j.start_local(kind="hunter", title="sleeper",
                            argv=[sys.executable, "-c", "import time; time.sleep(0.3)"])
        self.assertTrue(res["ok"])
        pid = res["job"]["pid"]
        self.assertIn(pid, jobs_mod.tracked_local_pids())
        # Wait for reaper thread to complete
        for _ in range(40):
            if pid not in jobs_mod.tracked_local_pids():
                break
            time.sleep(0.05)
        self.assertNotIn(pid, jobs_mod.tracked_local_pids())

        # Cross-instance reservation blocks concurrent exclusive job while status='starting'
        sock = Path(self.cfg.docker_sock)
        sock.write_text("")
        j1 = Jobs(self.cfg)
        j2 = Jobs(self.cfg)
        err = j1._guard_and_reserve(
            {"id": "res-1", "kind": "script", "title": "m", "argv": ["x"],
             "mode": "run", "runner": "beets", "log": "l", "started_ts": int(time.time()),
             "meta": {"exclusive": "maint"}},
            "beets", "maint",
        )
        self.assertIsNone(err)
        r2 = j2.start_beets(kind="script", title="m2", argv=["/bin/true"], exclusive="maint")
        self.assertFalse(r2["ok"])
        self.assertIn("already running", r2["error"])
        j1._release_reservation("res-1")
        with mock.patch.object(j2.docker, "exec_run", side_effect=jobs_mod.DockerError("boom")):
            r3 = j2.start_beets(kind="script", title="m3", argv=["/bin/true"], exclusive="maint")
            self.assertFalse(r3["ok"])
        self.assertIsNone(j2._fetch("res-1"))

    def test_finished_list_excludes_starting_and_refresh_sweeps_stale_starting(self) -> None:
        j = Jobs(self.cfg)
        j._guard_and_reserve(
            {"id": "st-fresh", "kind": "script", "title": "f", "argv": ["x"],
             "mode": "run", "runner": "beets", "log": "l", "started_ts": int(time.time()),
             "meta": {}},
            "beets", None,
        )
        j._guard_and_reserve(
            {"id": "st-stale", "kind": "script", "title": "s", "argv": ["x"],
             "mode": "run", "runner": "beets", "log": "l", "started_ts": int(time.time()) - 300,
             "meta": {}},
            "beets", None,
        )
        # status="finished" must NOT include active 'starting' reservations
        self.assertEqual(j.list(status="finished", refresh=False), [])
        # refresh() sweeps stale 'starting' (>120s) to 'failed' while leaving fresh 'starting'
        j.refresh()
        self.assertEqual(j._fetch("st-stale")["status"], "failed")
        self.assertEqual(j._fetch("st-fresh")["status"], "starting")


if __name__ == "__main__":
    unittest.main()

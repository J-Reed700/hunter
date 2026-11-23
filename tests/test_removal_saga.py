"""The removal saga: reserve at start, confirm or compensate on the outcome."""
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter import libops
from hunter.jobs import Jobs
from hunter.state import HunterState


def _want(st, key, artist, album, status="wanted"):
    now = int(time.time())
    st._conn.execute(
        "INSERT INTO wants (spotify_album_id, artist, album, source, status, created_ts, updated_ts)"
        " VALUES (?,?,?,?,?,?,?)", (key, artist, album, "gapfill", status, now, now))
    st._conn.commit()


class ParkRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = HunterState(Path(self.tmp.name) / "hunter.db")

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def _status(self, key):
        return self.st._conn.execute("SELECT status, note FROM wants WHERE spotify_album_id=?", (key,)).fetchone()

    def test_park_is_exact_and_restore_is_marker_scoped(self):
        _want(self.st, "k1", "Drake", "Scorpion")
        _want(self.st, "k2", "Drake_", "Underscore is not a wildcard")
        _want(self.st, "gapfill:beets:7", "Someone", "Album 7")
        _want(self.st, "k4", "Other", "Untouched")
        _want(self.st, "k5", "Other", "Parked by hand", status="skipped")
        n = self.st.park_wants(["Drake"], ["gapfill:beets:7"], "parked by removal job J")
        self.assertEqual(n, 2)
        self.assertEqual(tuple(self._status("k1")), ("skipped", "parked by removal job J"))
        self.assertEqual(self._status("k2")["status"], "wanted")
        self.assertEqual(self._status("gapfill:beets:7")["status"], "skipped")
        restored = self.st.restore_parked("parked by removal job J")
        self.assertEqual(restored, 2)
        self.assertEqual(tuple(self._status("k1")), ("wanted", ""))
        self.assertEqual(self._status("k5")["status"], "skipped")   # someone else's park survives


class FinishRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.cfg = SimpleNamespace(state_db=Path(d) / "hunter.db", quarantine_dir=Path(d) / "q",
                                   beets_automation_dir=Path(d) / "auto", log_dir=Path(d) / "logs",
                                   beets_db=Path(d) / "beets.db", docker_sock=f"{d}/no.sock",
                                   beets_container="beets", beets_exec_user="abc",
                                   music_root=Path(d) / "merged")
        (Path(d) / "q").mkdir()
        self.st = HunterState(self.cfg.state_db)
        _want(self.st, "k1", "Drake", "Scorpion")
        self.st.park_wants(["Drake"], [], "parked by removal job J")

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def _job(self, status, exit_code, result: dict | None):
        log = Path(self.tmp.name) / "job.log"
        lines = ["# job"]
        if result is not None:
            lines.append("RESULT " + json.dumps(result))
        lines.append(f"exit={exit_code}")
        log.write_text("\n".join(lines) + "\n")
        return {"id": "J", "kind": "library", "title": "Remove Drake", "status": status,
                "exit_code": exit_code, "log": str(log), "started_ts": int(time.time()),
                "meta": {"op": "remove", "side": {"drop_wants": True, "unfollow": True,
                                                   "artists": ["Drake"], "park_keys": [],
                                                   "label": "Drake", "requested_by": "admin"}}}

    def _events(self):
        return [(r["kind"], r["artist"]) for r in self.st.events(20)]

    def test_refusal_compensates(self):
        job = self._job("failed", 3, {"ok": False, "error": "quarantine: unreachable"})
        libops._finish_removal(self.cfg, job, job["meta"])
        row = self.st._conn.execute("SELECT status FROM wants WHERE spotify_album_id='k1'").fetchone()
        self.assertEqual(row["status"], "wanted")
        self.assertIn(("removal_failed", "Drake"), self._events())
        self.assertNotIn(("removed", "Drake"), self._events())

    def test_success_confirms(self):
        job = self._job("ok", 0, {"ok": True, "moved": 227, "albums_removed": 31, "run_dir": "/q/x",
                                  "blocked": ["Drake"], "errors": 0})
        libops._finish_removal(self.cfg, job, job["meta"])
        row = self.st._conn.execute("SELECT status FROM wants WHERE spotify_album_id='k1'").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertIn(("removed", "Drake"), self._events())


class LibOpsHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.cfg = SimpleNamespace(
            state_db=d / "hunter.db",
            quarantine_dir=d / "q",
            beets_automation_dir=d / "auto",
            log_dir=d / "logs",
            beets_db=d / "beets.db",
            docker_sock=str(d / "docker.sock"),
            beets_container="beets",
            beets_exec_user="abc",
            music_root=d / "merged",
        )
        (d / "q" / "library_prune").mkdir(parents=True)
        (d / "auto" / "lib").mkdir(parents=True)
        (d / "auto" / "lib" / "artist_blocklist.txt").write_text("", encoding="utf-8")
        con = sqlite3.connect(self.cfg.beets_db)
        con.executescript(
            "CREATE TABLE albums (id INTEGER PRIMARY KEY, albumartist TEXT, album TEXT, year INTEGER, mb_releasegroupid TEXT, artpath BLOB);"
            "CREATE TABLE items (id INTEGER PRIMARY KEY, album_id INTEGER, albumartist TEXT, artist TEXT, album TEXT, title TEXT, length REAL, bitrate INTEGER, format TEXT);"
        )
        for aid in range(1, 46):
            rgid = "rg-1" if aid == 1 else ""
            con.execute("INSERT INTO albums VALUES (?, '-M-', ?, 2020, ?, X'')", (aid, f"Alb {aid}", rgid))
            con.execute("INSERT INTO items VALUES (?, ?, '-M-', '-M-', ?, 'T', 100.0, 320000, 'FLAC')", (aid, aid, f"Alb {aid}"))
        con.commit()
        con.close()
        libops._PREFLIGHT.update(ts=0.0, res=None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_removal_plan_keeps_all_album_ids_and_artpath_blob(self):
        plan = libops.removal_plan(self.cfg, ["-M-"], [], [], sample=10)
        self.assertEqual(len(plan["albums"]), 10)
        self.assertEqual(len(plan["all_album_ids"]), 45)
        self.assertFalse(plan["albums"][0]["has_art"])

    def test_start_removal_reserves_first_and_compensates_on_launch_fail(self):
        st = HunterState(self.cfg.state_db)
        _want(st, "k_m", "-M-", "Qui de nous deux")
        st.close()

        plan = libops.removal_plan(self.cfg, ["-M-"], [], [])
        jobs = Jobs(self.cfg)
        with mock.patch.object(jobs, "start_beets", return_value={"ok": False, "error": "busy"}) as sb:
            res = libops.start_removal(self.cfg, jobs, plan, block=False, reason="-test-", requested_by="admin")
        self.assertFalse(res["ok"])
        # Compensated back to wanted!
        st2 = HunterState(self.cfg.state_db)
        row = st2._conn.execute("SELECT status FROM wants WHERE spotify_album_id='k_m'").fetchone()
        st2.close()
        self.assertEqual(row["status"], "wanted")
        argv = sb.call_args.kwargs["argv"]
        self.assertIn("--artist=-M-", argv)
        self.assertIn("--reason=-test-", argv)
        self.assertIn("--job-id", argv)
        park_keys = sb.call_args.kwargs["meta"]["side"]["park_keys"]
        self.assertIn("recovery:a1", park_keys)
        self.assertIn("request:rg:rg-1", park_keys)
        self.assertIn("release:rg:rg-1", park_keys)

    def test_run_for_job_matches_by_exact_job_id_and_new_run_re(self):
        run_dir = self.cfg.quarantine_dir / "library_prune" / "20261122_010203_a1b2_artist"
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(json.dumps({
            "id": "20261122_010203_a1b2_artist",
            "job_id": "job-exact-1",
            "ts": int(time.time()),
            "requested_by": "admin",
            "plan": {"artists": ["Artist"], "bytes": 10},
            "targets": {"artists": ["Artist"]},
            "items": [{"moved": True}],
            "albums": [],
            "result": {"moved": 1, "albums_removed": 0, "bytes": 10},
        }))
        self.assertIsNotNone(libops.removal(self.cfg, "20261122_010203_a1b2_artist"))
        got = libops._run_for_job(self.cfg, {"id": "job-exact-1", "started_ts": int(time.time())}, {"artists": ["Other"]})
        self.assertIsNotNone(got)
        self.assertEqual(got["id"], "20261122_010203_a1b2_artist")

    def test_blocklist_edit_uses_double_dash_and_delta_normalizes(self):
        Path(self.cfg.docker_sock).write_text("")
        docker = mock.MagicMock()
        docker.available = True
        docker.exec_run.return_value = {"exit_code": 0, "output": json.dumps({"ok": True, "added": ["-M-"], "count": 1})}
        res = libops.blocklist_edit(self.cfg, docker, add=["-M-"], remove=[])
        self.assertTrue(res["ok"])
        cmd = docker.exec_run.call_args.args[1]
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-1], "-M-")

        st = HunterState(self.cfg.state_db)
        _want(st, "k_tt", "The The", "Soul Mining")
        _want(st, "k_ok", "Other", "Keep")
        st.close()
        dropped = libops.blocklist_delta_for_wants(self.cfg, ["The The"])
        self.assertEqual(dropped, 1)

    def test_preflight_never_caches_failures_and_checks_need_bytes(self):
        Path(self.cfg.docker_sock).write_text("")
        with mock.patch("hunter.libops.DockerClient") as dc_cls:
            dc = dc_cls.return_value
            dc.available = True
            dc.exec_run.side_effect = [
                {"exit_code": 3, "output": 'RESULT {"ok": false, "error": "down"}'},
                {"exit_code": 0, "output": 'RESULT {"ok": true, "quarantine": {"free_bytes": 500}}'},
            ]
            r1 = libops.removal_preflight(self.cfg)
            self.assertFalse(r1["ok"])
            # Failure wasn't cached, so second call hits docker again
            r2 = libops.removal_preflight(self.cfg, need_bytes=1000)
            self.assertFalse(r2["ok"])
            self.assertIn("free space", r2["error"])


if __name__ == "__main__":
    unittest.main()

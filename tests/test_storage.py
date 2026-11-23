import errno
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hunter.storage import MountProbe, enclosing_mount, probe, summary

MOUNTINFO = """\
22 1 0:21 / / rw,relatime - overlay overlay rw
40 22 0:38 / /merged rw,nosuid - fuse.mergerfs mergerfs rw,user_id=0
41 22 0:39 / /merged rw,nosuid - fuse.mergerfs mergerfs rw,user_id=0
50 22 0:40 / /with\\040space rw - ext4 /dev/sdz1 rw
"""


class EnclosingMountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mi = os.path.join(self.tmp.name, "mountinfo")
        Path(self.mi).write_text(MOUNTINFO)

    def tearDown(self):
        self.tmp.cleanup()

    def test_deepest_prefix_wins(self):
        self.assertEqual(enclosing_mount("/merged/quarantine/music", self.mi), ("/merged", "fuse.mergerfs"))
        self.assertEqual(enclosing_mount("/etc/hosts", self.mi), ("/", "overlay"))

    def test_prefix_is_path_aware(self):
        self.assertEqual(enclosing_mount("/mergedx/a", self.mi), ("/", "overlay"))

    def test_unescapes_octal_space(self):
        self.assertEqual(enclosing_mount("/with space/x", self.mi), ("/with space", "ext4"))


class ProbeTests(unittest.TestCase):
    def test_missing_leaf_under_live_ancestor(self):
        with tempfile.TemporaryDirectory() as d:
            p = probe(os.path.join(d, "not", "here"))
            self.assertTrue(p.live)
            self.assertFalse(p.exists)
            self.assertIn("does not exist", p.reason(None) or "")
            self.assertIsNone(p.reason(None, must_exist=False))

    def test_symlink_is_followed(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real"); os.mkdir(real)
            link = os.path.join(d, "link"); os.symlink(real, link)
            p = probe(link)
            self.assertEqual(p.resolved, os.path.realpath(real))
            self.assertTrue(p.exists and p.live)

    def test_dead_bind_reason(self):
        dead = MountProbe(path="/quarantine", resolved="/quarantine", mountpoint="/quarantine",
                          fstype="fuse.mergerfs", exists=False, live=False, errno=107,
                          error="ENOTCONN: Socket not connected", free_bytes=None)
        self.assertIn("stale storage mount", dead.reason("fuse.mergerfs") or "")

    def test_summary_flags_wrong_fstype(self):
        with tempfile.TemporaryDirectory() as d:
            mi = os.path.join(d, "mountinfo")
            Path(mi).write_text("22 1 0:21 / / rw - overlay overlay rw\n")
            cfg = SimpleNamespace(music_root=Path(d), quarantine_dir=Path(d) / "q")
            s = summary(cfg, required_fstype="fuse.mergerfs", mountinfo=mi)
            self.assertFalse(s["ok"])
            self.assertIn("expected fuse.mergerfs", s["error"])
            ok = summary(cfg, required_fstype=None, mountinfo=mi)
            self.assertFalse(ok["ok"])            # quarantine dir does not exist yet
            self.assertIn("does not exist", ok["error"])
            os.mkdir(os.path.join(d, "q"))
            self.assertTrue(summary(cfg, required_fstype=None, mountinfo=mi)["ok"])

    def test_summary_collects_all_root_errors_and_reads_cfg_storage_fstype(self):
        with tempfile.TemporaryDirectory() as d:
            mi = os.path.join(d, "mountinfo")
            Path(mi).write_text("22 1 0:21 / / rw - overlay overlay rw\n")
            cfg = SimpleNamespace(
                music_root=Path(d) / "missing_m",
                quarantine_dir=Path(d) / "missing_q",
                storage_fstype=None,
            )
            s = summary(cfg, mountinfo=mi)
            self.assertFalse(s["ok"])
            self.assertIn("merged:", s["error"])
            self.assertIn("quarantine:", s["error"])

    def test_summary_skips_missing_quarantine_when_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            mi = os.path.join(d, "mountinfo")
            Path(mi).write_text("22 1 0:21 / / rw - overlay overlay rw\n")
            cfg = SimpleNamespace(
                music_root=Path(d),
                quarantine_dir=Path(d) / "missing_q",
                quarantine_enabled=False,
                storage_fstype=None,
            )
            s = summary(cfg, mountinfo=mi)
            self.assertTrue(s["ok"])
            self.assertEqual(
                s["quarantine"],
                {
                    "path": str(Path(d) / "missing_q"),
                    "configured": False,
                    "live": True,
                    "exists": False,
                    "error": None,
                },
            )

    def test_cross_mount_ancestor_skips_statvfs_and_enotconn_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            mi = os.path.join(d, "mountinfo")
            missing_sub = os.path.join(d, "unmounted_sub", "leaf")
            Path(mi).write_text(
                f"22 1 0:21 / / rw - overlay overlay rw\n"
                f"40 22 0:38 / {os.path.realpath(d)}/unmounted_sub rw - fuse.mergerfs mergerfs rw\n"
            )
            p = probe(missing_sub, mountinfo=mi)
            self.assertTrue(p.live)
            self.assertFalse(p.exists)
            self.assertEqual(p.mountpoint, f"{os.path.realpath(d)}/unmounted_sub")
            self.assertIsNone(p.free_bytes)

            with mock.patch(
                "hunter.storage.os.path.realpath",
                side_effect=OSError(errno.ENOTCONN, "Transport endpoint is not connected"),
            ):
                bad = probe("/quarantine", mountinfo=mi)
                self.assertFalse(bad.live)
                self.assertEqual(bad.errno, errno.ENOTCONN)
                self.assertIn("ENOTCONN", bad.error)


if __name__ == "__main__":
    unittest.main()

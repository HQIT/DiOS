import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.runtime.database import RuntimeErrorCode
from app.runtime.linux_cgroup import kill_frozen_container


class FrozenTerminationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.proc, self.cg = root / "proc", root / "cgroup"
        self.id = "a" * 64
        self.target = self.cg / "system.slice" / f"docker-{self.id}.scope"
        self.target.mkdir(parents=True)
        (self.proc / "42").mkdir(parents=True)
        self.membership = self.proc / "42/cgroup"
        self.membership.write_text(f"0::/system.slice/docker-{self.id}.scope\n")
        (self.target / "cgroup.events").write_text("populated 1\nfrozen 1\n")
        (self.target / "cgroup.kill").write_text("0")

    def kill(self):
        kill_frozen_container(self.id, 42, proc_root=self.proc, cgroup_root=self.cg)

    def test_owned_frozen_group_uses_native_kill(self):
        self.kill()
        self.assertEqual((self.target / "cgroup.kill").read_text(), "1")

    def test_other_container_or_remote_pid_is_rejected(self):
        self.membership.write_text("0::/system.slice/docker-" + "b" * 64 + ".scope\n")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RUNTIME_FROZEN_STOP_UNAVAILABLE"
        ):
            self.kill()
        self.assertEqual((self.target / "cgroup.kill").read_text(), "0")

    def test_unconfirmed_freeze_is_rejected(self):
        (self.target / "cgroup.events").write_text("populated 1\nfrozen 0\n")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RUNTIME_FROZEN_STOP_UNAVAILABLE"
        ):
            self.kill()

    def test_missing_kill_interface_is_rejected(self):
        (self.target / "cgroup.kill").unlink()
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RUNTIME_FROZEN_STOP_UNAVAILABLE"
        ):
            self.kill()

    def test_kill_symlink_is_rejected(self):
        (self.target / "cgroup.kill").unlink()
        elsewhere = self.cg / "unrelated"
        elsewhere.write_text("0")
        (self.target / "cgroup.kill").symlink_to(elsewhere)
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RUNTIME_FROZEN_STOP_UNAVAILABLE"
        ):
            self.kill()
        self.assertEqual(elsewhere.read_text(), "0")

    def test_no_host_permission_keeps_group_untouched(self):
        with (
            patch("app.runtime.linux_cgroup.os.open", side_effect=PermissionError),
            self.assertRaisesRegex(RuntimeErrorCode, "RUNTIME_FROZEN_STOP_UNAVAILABLE"),
        ):
            self.kill()
        self.assertEqual((self.target / "cgroup.kill").read_text(), "0")

    def test_descendant_membership_cannot_claim_whole_container_exit(self):
        self.membership.write_text(f"0::/system.slice/docker-{self.id}.scope/child\n")
        with self.assertRaisesRegex(
            RuntimeErrorCode, "RUNTIME_FROZEN_STOP_UNAVAILABLE"
        ):
            self.kill()
        self.assertEqual((self.target / "cgroup.kill").read_text(), "0")

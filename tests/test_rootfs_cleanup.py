import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/rootfs-cleanup.py"


@unittest.skipUnless(os.geteuid() == 0, "requires root for ownership checks")
class CleanupTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "rootfs"
        self.lease = self.base / "lease"
        self.lease.write_bytes(os.fsencode(self.root))
        self.lease.chmod(0o600)
        for name in ("etc", "usr", "var", "root", "home", "run", "tmp", "var/tmp"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.put("etc/passwd", "root:x:0:0:root:/root:/bin/sh\n")
        self.put("etc/shadow", "root:!:0:0:99999:7:::\n")
        self.put("var/lib/dpkg/status", "Package: dpkg\nStatus: install ok installed\nVersion: 1\n\n")
        self.put("etc/hostname", "builder\n")
        self.put("etc/resolv.conf", "nameserver 10.0.0.2\n")
        self.put("etc/subuid", "# keep\nroot:100000:65536\nuser:200000:65536\n")
        self.put("var/log/test.log", "temporary\n")
        self.put("tmp/.hidden", "temporary\n")
        self.put("usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n")

    def put(self, name, content):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def execute(self, stage, expect=0, **options):
        env = dict(os.environ)
        for key in ("ROOTFS_BUILD_DPKG_OPTIONS", "ROOTFS_REQUIRED_PACKAGES"):
            env.pop(key, None)
        env["ROOTFS_CLEANUP_LEASE"] = str(self.lease)
        env.update(options)
        result = subprocess.run([sys.executable, str(SCRIPT), stage, str(self.root)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expect, result.stdout + result.stderr)
        return result

    def test_two_passes(self):
        self.execute("t1")
        self.assertTrue((self.root / "usr/sbin/policy-rc.d").exists())
        self.put("etc/hostname", "restored\n")
        self.execute("t2")
        self.execute("t2")
        self.execute("verify")
        self.assertEqual((self.root / "etc/hostname").read_bytes(), b"")
        self.assertEqual((self.root / "etc/subuid").read_text(), "# keep\nuser:200000:65536\n")
        self.assertFalse((self.root / "usr/sbin/policy-rc.d").exists())
        self.assertFalse((self.root / "tmp/.hidden").exists())
        self.assertFalse((self.root / "var/lib/incus").exists())

    def test_missing_or_mismatched_authorization(self):
        self.execute("t2", 95, ROOTFS_CLEANUP_LEASE="")
        self.lease.write_text("/not-this-build")
        self.execute("t2", 95)
        self.assertEqual((self.root / "etc/hostname").read_text(), "builder\n")

    def test_verify_read_only(self):
        self.execute("t2")
        p = self.put("etc/hostname", "bad\n")
        self.execute("verify", 95)
        self.assertEqual(p.read_text(), "bad\n")

    def test_links(self):
        p = self.root / "etc/hostname"
        outside = self.base / "outside"
        outside.write_text("untouched")
        p.unlink()
        p.symlink_to(outside)
        self.execute("t2", 95)
        p.unlink()
        os.link(outside, p)
        self.execute("t2", 95)
        self.assertEqual(outside.read_text(), "untouched")
        p.unlink()
        p.write_text("builder")
        outside_dir = self.base / "outside-dir"
        outside_dir.mkdir()
        (self.root / "var/lib/iscsi").symlink_to(outside_dir)
        self.execute("t2", 95)
        self.assertEqual(list(outside_dir.iterdir()), [])

    def test_iscsi_marker_and_version(self):
        p = self.root / "var/lib/dpkg/status"
        p.write_text(p.read_text() + "Package: open-iscsi\nStatus: install ok installed\nVersion: 2.1.11-1+deb13u2\n\n")
        self.put("usr/lib/open-iscsi/startup-checks.sh", "GenerateName=yes\niscsi-iname\nInitiatorName=\n")
        self.put("usr/sbin/iscsi-iname", "#!/bin/sh\n").chmod(0o755)
        q = self.put("etc/iscsi/initiatorname.iscsi", "InitiatorName=iqn.example:old\n")
        self.execute("t2")
        self.execute("verify")
        self.assertEqual(q.read_text(), "GenerateName=yes\n")
        self.assertEqual(q.stat().st_mode & 0o777, 0o600)
        p.write_text(p.read_text().replace("2.1.11-1+deb13u2", "2.1.13-1"))
        self.execute("t2", 95)

    def test_unknown_state_and_policy(self):
        p = self.put("etc/hostid", "unreviewed")
        self.execute("t2", 95)
        self.assertEqual(p.read_text(), "unreviewed")
        p.unlink()
        p = self.put("usr/sbin/policy-rc.d", "foreign")
        self.execute("t2", 95)
        self.assertEqual(p.read_text(), "foreign")

    def test_fragment_ownership(self):
        p = self.put("etc/dpkg/dpkg.cfg.d/99mmdebstrap", "path-exclude=/usr/share/man/*\n")
        self.execute("t1", 95)
        self.assertTrue(p.exists())
        self.execute("t1", ROOTFS_BUILD_DPKG_OPTIONS="path-exclude=/usr/share/man/*")
        self.assertFalse(p.exists())

    def test_mount_guard_includes_same_device_bind_mounts(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rootfs_cleanup", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from unittest.mock import patch
        with patch.dict(os.environ, {"ROOTFS_CLEANUP_LEASE": str(self.lease)}):
            cleaner = module.Cleanup(str(self.root), "t1")
        nested = self.root / "tmp/nested"
        cleaner.mounts.append(nested)
        with self.assertRaises(module.Invalid):
            cleaner.empty_dir("test", "tmp")
        self.assertTrue((self.root / "tmp/.hidden").exists())

    def test_boot_artifact_rejected(self):
        self.put("boot/initrd.img-test", "old IQN")
        self.execute("t2", 95)


if __name__ == "__main__":
    unittest.main()

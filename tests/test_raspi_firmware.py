"""Reject stale or mismatched firmware copies without any runner hardware probes."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("raspi_firmware", REPOSITORY / "scripts/raspi-firmware.py")
FIRMWARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIRMWARE)


class RaspiFirmwareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="raspi-firmware-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.version = "6.12.10+deb13-arm64"
        self.kernel(self.version)
        self.copy_pair(self.version)

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def kernel(self, version):
        for prefix in ("vmlinuz", "initrd.img"):
            self.write(f"boot/{prefix}-{version}", f"{prefix}:{version}".encode())
        self.write(f"usr/lib/linux-image-{version}/broadcom/bcm2711-rpi-4-b.dtb", b"fixture Pi4 DTB")
        self.write(f"usr/lib/linux-image-{version}/broadcom/bcm2711-rpi-cm4.dtb", b"fixture CM4 DTB")

    def copy_pair(self, version):
        for prefix in ("vmlinuz", "initrd.img"):
            name = f"{prefix}-{version}"
            self.write(f"boot/firmware/{name}", (self.root / f"boot/{name}").read_bytes())
        for source in (self.root / f"usr/lib/linux-image-{version}/broadcom").glob("*.dtb"):
            self.write(f"boot/firmware/{source.name}", source.read_bytes())
        self.write("boot/firmware/config.txt", (
            f"arm_64bit=1\nkernel=vmlinuz-{version}\ninitramfs initrd.img-{version}\n").encode())

    def test_paired_files_and_multiple_board_dtbs_are_accepted(self):
        record = FIRMWARE.verify(self.root)
        self.assertEqual(record['kernel'], self.version)
        self.assertEqual(len(record['dtbs']), 2)

    def test_cmdline_contents_are_outside_binary_acceptance(self):
        self.write("boot/firmware/cmdline.txt", b"root=build-environment-placeholder\n")
        self.assertEqual(FIRMWARE.verify(self.root)['kernel'], self.version)

    def test_missing_stale_or_empty_copy_is_rejected(self):
        for name in (f"initrd.img-{self.version}", f"vmlinuz-{self.version}", "bcm2711-rpi-4-b.dtb"):
            with self.subTest(name=name):
                self.copy_pair(self.version)
                path = self.root / f"boot/firmware/{name}"
                path.write_bytes(b"stale")
                with self.assertRaisesRegex(ValueError, "stale|DTB differs"):
                    FIRMWARE.verify(self.root)
                path.unlink()
                with self.assertRaises(FileNotFoundError):
                    FIRMWARE.verify(self.root)
                path.write_bytes(b"")
                with self.assertRaisesRegex(ValueError, "empty"):
                    FIRMWARE.verify(self.root)

    def test_versions_are_sorted_numerically_and_must_be_paired(self):
        self.kernel("6.12.9-fixture-arm64")
        self.assertEqual(FIRMWARE.verify(self.root)['kernel'], self.version)
        self.kernel("6.12.11-fixture-arm64")
        with self.assertRaisesRegex(ValueError, "matching target"):
            FIRMWARE.verify(self.root)
        self.copy_pair("6.12.11-fixture-arm64")
        self.assertEqual(FIRMWARE.verify(self.root)['kernel'], "6.12.11-fixture-arm64")

    def test_mismatched_initrd_reference_is_rejected(self):
        path = self.root / 'boot/firmware/config.txt'
        path.write_text(path.read_text().replace(f"initrd.img-{self.version}", "initrd.img-other"))
        with self.assertRaisesRegex(ValueError, 'matching target'):
            FIRMWARE.verify(self.root)

    def test_missing_packaged_dtb_set_is_rejected(self):
        for path in (self.root / f"usr/lib/linux-image-{self.version}/broadcom").glob("*.dtb"):
            path.unlink()
        with self.assertRaisesRegex(ValueError, "no packaged"):
            FIRMWARE.verify(self.root)

    def test_symlink_outside_rootfs_is_rejected(self):
        copy = self.root / f"boot/firmware/vmlinuz-{self.version}"
        copy.unlink()
        copy.symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'escaping'):
            FIRMWARE.verify(self.root)

    def test_optional_check_queries_real_dpkg_status(self):
        self.write('var/lib/dpkg/status', b'')
        self.assertFalse(FIRMWARE.installed(self.root))
        # Other base recipes need no /boot/firmware; keep that path optional.
        result = subprocess.run([sys.executable, str(REPOSITORY/'scripts/raspi-firmware.py'),
                                 str(self.root), '--if-installed'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn('package absent', result.stdout)
        self.write('var/lib/dpkg/status', b'Package: raspi-firmware\nStatus: install ok installed\n'
                   b'Architecture: all\nVersion: 1\nDescription: fixture\n\n')
        self.assertTrue(FIRMWARE.installed(self.root))

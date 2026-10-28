import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rootfs_target", REPOSITORY / "scripts/target.py")
TARGET = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TARGET)
TARGET_ID = "amd64-trixie-memini"


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rootfs-target-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_registered_memini_target(self):
        target = TARGET.resolve(TARGET_ID)
        self.assertEqual(target["artifact_name"], "rootfs-trixie-amd64-memini")
        self.assertEqual(target["execution"], "native")
        self.assertIn("@sha256:", target["builder_image"])

    def test_unknown_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown target"):
            TARGET.resolve("unregistered-target")

    def registry(self, **changes):
        target = json.loads((REPOSITORY / "targets/targets.json").read_text())[TARGET_ID]
        target.update(changes)
        (self.base / "targets").mkdir()
        (self.base / "targets/targets.json").write_text(json.dumps({TARGET_ID: target}))
        recipe = self.base / "recipes/amd64/trixie/memini/recipe.sh"
        recipe.parent.mkdir(parents=True)
        recipe.write_text("#!/bin/sh\nexit 0\n")
        recipe.chmod(0o755)

    def test_recipe_cannot_escape_registry_layout(self):
        self.registry(recipe="../../outside.sh")
        with mock.patch.object(TARGET, "REPOSITORY", self.base), self.assertRaises(ValueError):
            TARGET.resolve(TARGET_ID)

    def test_unsupported_backend_is_rejected(self):
        self.registry(execution="qemu")
        with mock.patch.object(TARGET, "REPOSITORY", self.base), self.assertRaisesRegex(ValueError, "unsupported"):
            TARGET.resolve(TARGET_ID)

    def test_unpinned_builder_is_rejected(self):
        self.registry(builder_image="debian:trixie-slim")
        with mock.patch.object(TARGET, "REPOSITORY", self.base), self.assertRaisesRegex(ValueError, "digest-pinned"):
            TARGET.resolve(TARGET_ID)

    def test_output_injection_is_rejected(self):
        self.registry(runner="ubuntu-24.04\nrunner=other")
        with mock.patch.object(TARGET, "REPOSITORY", self.base), self.assertRaisesRegex(ValueError, "single-line"):
            TARGET.resolve(TARGET_ID)

    def test_metadata_exports_real_package_status_and_checksums(self):
        root = self.base / "rootfs"
        database = root / "var/lib/dpkg"
        database.mkdir(parents=True)
        database.joinpath("status").write_text(
            "Package: zfs-dkms\nStatus: install ok installed\nArchitecture: all\n"
            "Version: 2.3-fixture\nDescription: fixture ZFS package\n\n"
            "Package: removed\nStatus: deinstall ok config-files\nArchitecture: amd64\n"
            "Version: 1\nDescription: fixture removed package\n\n")
        (root / "lib/modules/6.12-fixture-amd64").mkdir(parents=True)
        output = self.base / "output"
        output.mkdir()
        stem = "rootfs-trixie-amd64-memini"
        archive = output / f"{stem}.tar.zst"
        archive.write_bytes(b"metadata unit test: archive was validated by its caller")
        (output / f"{stem}.tar.zst.sha256").write_text(
            f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {archive.name}\n")
        tools = self.base / "tools"
        tools.mkdir()
        mmdebstrap = tools / "mmdebstrap"
        mmdebstrap.write_text("#!/bin/sh\nprintf 'mmdebstrap version fixture\\n'\n")
        mmdebstrap.chmod(0o755)
        with mock.patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                             SOURCE_COMMIT="a" * 40, SOURCE_REPOSITORY="test/repo"):
            TARGET.export_metadata(TARGET_ID, root, output)
        metadata = json.loads((output / f"{stem}.build.json").read_text())
        self.assertEqual(metadata["source_commit"], "a" * 40)
        self.assertEqual(metadata["target_kernels"], ["6.12-fixture-amd64"])
        self.assertEqual(metadata["zfs_packages"], {"zfs-dkms": "2.3-fixture"})
        self.assertEqual(metadata["installed_package_count"], 1)
        self.assertFalse(metadata["boot_tested"])
        self.assertEqual((output / f"{stem}.packages.tsv").read_text(),
                         "package\tversion\tarchitecture\nzfs-dkms\t2.3-fixture\tall\n")
        for line in (output / f"{stem}.SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)

    def test_diagnostics_copy_logs_without_following_rootfs_symlinks(self):
        state = self.base / "state"
        work = state / "work/rootfs-build.fixture"
        work.mkdir(parents=True)
        (work / "build.log").write_text("build log")
        make_log = work / "rootfs/var/lib/dkms/zfs/2.3/build/make.log"
        make_log.parent.mkdir(parents=True)
        make_log.write_text("compiler log")
        installed_log = work / "rootfs/var/lib/dkms/zfs/2.3/6.12-fixture/amd64/log/make.log"
        installed_log.parent.mkdir(parents=True)
        installed_log.write_text("installed module log")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "dpkg.log").write_text("must not be copied")
        var = work / "rootfs/var"
        (var / "log").symlink_to(outside, target_is_directory=True)
        TARGET.collect_diagnostics(state)
        self.assertEqual((state / "logs/work/rootfs-build.fixture/build.log").read_text(), "build log")
        self.assertEqual((state / "logs" / make_log.relative_to(state)).read_text(), "compiler log")
        self.assertEqual((state / "logs" / installed_log.relative_to(state)).read_text(), "installed module log")
        self.assertFalse((state / "logs/work/rootfs-build.fixture/rootfs/var/log/dpkg.log").exists())


if __name__ == "__main__":
    unittest.main()

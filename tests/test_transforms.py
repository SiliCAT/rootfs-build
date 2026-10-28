"""Artifact selection, input integrity and final transform provenance without network."""

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request
import zipfile

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / "scripts"))
SPEC = importlib.util.spec_from_file_location("rootfs_transform", REPOSITORY / "scripts/transform.py")
TRANSFORM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRANSFORM)


class TransformTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rootfs-transform-tests-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.run = {"id": 123, "status": "completed", "conclusion": "success",
                    "repository": {"full_name": "SiliCAT/rootfs-build"},
                    "head_repository": {"full_name": "SiliCAT/rootfs-build"},
                    "path": ".github/workflows/rootfs.yml", "head_sha": "a" * 40, "run_attempt": 2}
        self.artifact = {"id": 456, "expired": False, "workflow_run": {"id": 123},
                         "name": "rootfs-trixie-amd64-vm-incusd-123-1", "digest": None}
        self.source = self.inspect()
        self.directory = self.base / "input"
        self.directory.mkdir()
        self.stem = self.source["base_target"]["artifact_name"]
        self.archive = self.directory / f"{self.stem}.tar.zst"
        self.archive.write_bytes(b"fixture archive; actual tar handling has separate tests")
        build = {"target": self.source["base_target"], "source_repository": self.source["repository"],
                 "source_commit": self.source["source_commit"], "workflow_run_url": self.source["run_url"],
                 "archive": self.archive.name, "archive_sha256": TRANSFORM.target.sha256(self.archive)}
        (self.directory / f"{self.stem}.build.json").write_text(json.dumps(build))
        (self.directory / f"{self.stem}.packages.tsv").write_text("package\tversion\tarchitecture\n")
        (self.directory / f"{self.archive.name}.sha256").write_text(
            f"{build['archive_sha256']}  {self.archive.name}\n")
        self.resum()
        (self.directory / "source.json").write_text(json.dumps(self.source))

    def inspect(self):
        with mock.patch.object(TRANSFORM, "api_json", side_effect=[
                self.run, self.artifact, dict(self.run, run_attempt=1)]):
            return TRANSFORM.inspect_source("SiliCAT/rootfs-build", "123", "456")

    def resum(self):
        names = sorted(TRANSFORM.expected_files(self.source) - {f"{self.stem}.SHA256SUMS"})
        (self.directory / f"{self.stem}.SHA256SUMS").write_text("".join(
            f"{TRANSFORM.target.sha256(self.directory / name)}  {name}\n" for name in names))

    def verify(self):
        return TRANSFORM.verify_input(self.directory, self.source)

    def test_exact_artifact_and_attempt_are_preserved(self):
        self.assertEqual(self.source["run_attempt"], 1)
        self.assertEqual(self.source["artifact_id"], 456)
        self.assertEqual(self.source["backend"]["platform"], "linux/amd64")
        self.assertEqual(self.verify()["archive_sha256"], TRANSFORM.target.sha256(self.archive))

    def test_run_must_be_successful_in_this_repository_and_base_workflow(self):
        for changes in ({"status": "in_progress"}, {"conclusion": "failure"},
                        {"path": ".github/workflows/rootfs-transform.yml"},
                        {"head_repository": {"full_name": "outside/fork"}},
                        {"repository": {"full_name": "outside/repo"}}):
            with self.subTest(changes=changes):
                original = self.run
                self.run = dict(original, **changes)
                with self.assertRaises(ValueError):
                    self.inspect()
                self.run = original

    def test_artifact_cannot_be_expired_foreign_logs_or_an_unknown_attempt(self):
        for changes in ({"expired": True}, {"workflow_run": {"id": 999}},
                        {"name": "logs-rootfs-trixie-amd64-vm-incusd-123-1"},
                        {"name": "rootfs-trixie-amd64-vm-incusd-123-3"}):
            with self.subTest(changes=changes):
                original = self.artifact
                self.artifact = dict(original, **changes)
                with self.assertRaises(ValueError):
                    self.inspect()
                self.artifact = original

    def test_a_successful_rerun_does_not_approve_an_artifact_from_a_failed_attempt(self):
        failed_attempt = dict(self.run, run_attempt=1, conclusion="failure")
        with mock.patch.object(TRANSFORM, "api_json", side_effect=[self.run, self.artifact, failed_attempt]):
            with self.assertRaisesRegex(ValueError, "attempt must also be successful"):
                TRANSFORM.inspect_source("SiliCAT/rootfs-build", "123", "456")

    def test_unsupported_architecture_has_no_implicit_fallback(self):
        for architecture in ("arm64", "armel", "unknown"):
            with self.subTest(architecture=architecture), self.assertRaisesRegex(ValueError, "backend"):
                TRANSFORM.backend(architecture)

    def test_derived_identity_uses_shared_recipe_and_retains_base_architecture(self):
        derived = TRANSFORM.derived_target(self.source)
        self.assertEqual(derived["architecture"], "amd64")
        self.assertEqual(derived["id"], "amd64-trixie-vm-incusd-br-initrd")
        self.assertEqual(derived["recipe"], "transforms/trixie/br-initrd/recipe.sh")
        self.assertEqual(self.source["base_target"]["name"], "vm-incusd")

    def test_checksum_corruption_is_rejected_before_execution(self):
        self.archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.verify()

    def test_valid_checksums_do_not_replace_source_provenance_checks(self):
        path = self.directory / f"{self.stem}.build.json"
        for changes in ({"source_commit": "b" * 40}, {"source_repository": "other/repo"},
                        {"workflow_run_url": "https://github.com/other/repo/actions/runs/123"},
                        {"archive_sha256": "0" * 64}, {"target": {"architecture": "arm64"}}):
            with self.subTest(changes=changes):
                original = path.read_text()
                path.write_text(json.dumps(dict(json.loads(original), **changes)))
                self.resum()
                with self.assertRaises(ValueError):
                    self.verify()
                path.write_text(original)
                self.resum()

    def test_checksum_manifest_cannot_reference_other_files(self):
        path = self.directory / f"{self.stem}.SHA256SUMS"
        path.write_text("0" * 64 + "  ../outside\n")
        with self.assertRaisesRegex(ValueError, "checksum entry"):
            self.verify()

    def test_input_symlinks_are_rejected(self):
        path = self.directory / f"{self.stem}.packages.tsv"
        outside = self.base / "outside"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "regular input"):
            self.verify()

    def zip_input(self, change=None):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as bundle:
            for name in sorted(TRANSFORM.expected_files(self.source)):
                if change and name == self.archive.name:
                    change(bundle, name)
                else:
                    bundle.writestr(name, (self.directory / name).read_bytes())
        data.seek(0)
        return data

    def test_artifact_download_unpack_accepts_only_exact_flat_regular_files(self):
        destination = self.base / "download"
        destination.mkdir()
        TRANSFORM.unpack_artifact(self.zip_input(), destination, self.source)
        self.assertEqual(TRANSFORM.verify_input(destination, self.source)["archive"], self.archive.name)
        self.assertEqual({p.name for p in destination.iterdir()}, TRANSFORM.expected_files(self.source))

    def test_artifact_zip_rejects_path_traversal_and_symlinks(self):
        def traversal(bundle, name):
            bundle.writestr("../" + name, b"bad")

        def symlink(bundle, name):
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(entry, "/etc/passwd")

        for index, change in enumerate((traversal, symlink)):
            destination = self.base / f"bad-{index}"
            destination.mkdir()
            with self.subTest(index=index), self.assertRaises(ValueError):
                TRANSFORM.unpack_artifact(self.zip_input(change), destination, self.source)
            self.assertEqual(list(destination.iterdir()), [])

    def test_signed_download_redirect_does_not_receive_github_token(self):
        request = urllib.request.Request("https://api.github.com/repos/a/b/actions/artifacts/1/zip",
                                         headers={"Authorization": "Bearer fixture-secret"})
        redirected = TRANSFORM.SafeRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://artifact.example.test/download")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            TRANSFORM.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "http://bad.test/a")

    def test_source_is_revalidated_before_downloading(self):
        changed = dict(self.source, source_commit="b" * 40)
        with mock.patch.object(TRANSFORM, "inspect_source", return_value=changed), \
                mock.patch.object(TRANSFORM, "github_request") as request:
            with self.assertRaisesRegex(ValueError, "changed since resolution"):
                TRANSFORM.fetch_source(self.source, self.base / "download")
            request.assert_not_called()

    def test_fetch_verifies_digest_and_complete_input(self):
        data = self.zip_input().getvalue()
        source = dict(self.source, artifact_digest="sha256:" + hashlib.sha256(data).hexdigest())
        with mock.patch.object(TRANSFORM, "inspect_source", return_value=source), \
                mock.patch.object(TRANSFORM, "github_request", return_value=io.BytesIO(data)):
            result = TRANSFORM.fetch_source(source, self.base / "download")
        self.assertEqual(result["archive"], self.archive.name)
        self.assertFalse((self.base / "download/artifact.zip.part").exists())

    def test_export_binds_input_and_final_initramfs_and_uses_final_package_database(self):
        root = self.base / "root"
        (root / "boot").mkdir(parents=True)
        kernel = "6.12-fixture-amd64"
        image = root / f"boot/initrd.img-{kernel}"
        image.write_bytes(b"final rebuilt initramfs")
        (root / f"lib/modules/{kernel}").mkdir(parents=True)
        database = root / "var/lib/dpkg"
        database.mkdir(parents=True)
        (database / "status").write_text(
            "Package: final-package\nStatus: install ok installed\nArchitecture: amd64\n"
            "Version: 2\nDescription: final fixture\n\n")
        report = self.base / "report.json"
        report.write_text(json.dumps({"transform": "br-initrd", "selection": "update-initramfs -u",
            "boot_tested": False, "accepted_initramfs": [{"kernel": kernel,
            "path": f"/boot/initrd.img-{kernel}", "sha256": TRANSFORM.target.sha256(image)}]}))
        output = self.base / "output"
        output.mkdir()
        stem = self.stem + "-br-initrd"
        archive = output / f"{stem}.tar.zst"
        archive.write_bytes(b"final archive validated by caller")
        (output / f"{archive.name}.sha256").write_text(
            f"{TRANSFORM.target.sha256(archive)}  {archive.name}\n")
        tools = self.base / "tools"
        tools.mkdir()
        (tools / "mmdebstrap").write_text("#!/bin/sh\nprintf 'mmdebstrap fixture\\n'\n")
        (tools / "mmdebstrap").chmod(0o755)
        with mock.patch.dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"]):
            TRANSFORM.export_transform(self.directory, root, output, report)
        metadata = json.loads((output / f"{stem}.build.json").read_text())
        self.assertEqual(metadata["input"]["artifact_id"], 456)
        self.assertEqual(metadata["base_build"]["source_commit"], "a" * 40)
        self.assertEqual(metadata["transform"]["accepted_initramfs"][0]["kernel"], kernel)
        self.assertEqual(metadata["installed_package_count"], 1)
        self.assertIn("final-package\t2\tamd64", (output / f"{stem}.packages.tsv").read_text())
        image.write_bytes(b"unexpected later modification")
        with self.assertRaisesRegex(ValueError, "changed after verification"):
            TRANSFORM.export_transform(self.directory, root, output, report)


if __name__ == "__main__":
    unittest.main()

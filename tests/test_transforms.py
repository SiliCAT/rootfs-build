"""Artifact selection, input integrity and final transform provenance without network."""

import importlib.util
from datetime import datetime, timezone
from contextlib import nullcontext, redirect_stderr, redirect_stdout
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


class InputCatalogTests(unittest.TestCase):
    def setUp(self):
        self.repository = "SiliCAT/rootfs-build"
        self.now = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
        self.run = {"id": 123, "status": "completed", "conclusion": "success",
                    "repository": {"full_name": self.repository},
                    "head_repository": {"full_name": self.repository},
                    "path": ".github/workflows/rootfs.yml", "head_sha": "a" * 40,
                    "run_attempt": 2}
        self.artifact = {"id": 456, "expired": False, "workflow_run": {"id": 123},
                         "name": "rootfs-trixie-amd64-vm-incusd-123-1", "digest": None,
                         "created_at": "2026-10-28T10:00:00Z", "expires_at": "2099-11-10T10:00:00Z"}
        self.calls = []

    def query(self, pages, runs=None, attempts=None, **limits):
        runs = {123: self.run} if runs is None else runs
        attempts = {(123, 1): dict(self.run, run_attempt=1)} if attempts is None else attempts

        def api(path, *, query=None):
            self.calls.append((path, query))
            prefix = f"/repos/{self.repository}/actions/"
            if path == prefix + "artifacts":
                return {"artifacts": pages[query['page'] - 1]}
            if query is not None:
                self.fail("pagination used outside the artifact listing")
            suffix = path.removeprefix(prefix).split('/')
            if len(suffix) == 2 and suffix[0] == 'runs':
                return runs[int(suffix[1])]
            if len(suffix) == 4 and suffix[0] == 'runs' and suffix[2] == 'attempts':
                return attempts[(int(suffix[1]), int(suffix[3]))]
            self.fail("unexpected API request: " + path)

        with mock.patch.object(TRANSFORM, "api_json", side_effect=api), \
                (mock.patch.multiple(TRANSFORM, **limits) if limits else nullcontext()):
            return TRANSFORM.list_inputs(self.repository, self.now)

    def test_artifact_id_alone_resolves_run_without_changing_the_descriptor(self):
        attempt = dict(self.run, run_attempt=1)
        with mock.patch.object(TRANSFORM, "api_json", side_effect=[self.artifact, self.run, attempt]) as api:
            inferred = TRANSFORM.inspect_source(self.repository, None, "456")
        with mock.patch.object(TRANSFORM, "api_json", side_effect=[self.run, self.artifact, attempt]):
            explicit = TRANSFORM.inspect_source(self.repository, "123", "456")
        self.assertEqual(inferred, explicit)
        self.assertEqual(api.call_args_list[0], mock.call(f"/repos/{self.repository}/actions/artifacts/456"))
        self.assertEqual(api.call_args_list[1], mock.call(f"/repos/{self.repository}/actions/runs/123"))

    def test_auto_resolution_rejects_artifacts_without_a_producing_run(self):
        with mock.patch.object(TRANSFORM, "api_json", return_value=dict(self.artifact, workflow_run=None)) as api:
            with self.assertRaisesRegex(TRANSFORM.SourceRejected, "producing workflow"):
                TRANSFORM.inspect_source(self.repository, None, "456")
            self.assertEqual(api.call_count, 1)

    def test_auto_resolution_rejects_expired_timestamps_without_selecting_another_artifact(self):
        expired = dict(self.artifact, expires_at="2000-01-01T00:00:00Z")
        with mock.patch.object(TRANSFORM, "api_json", side_effect=[expired, self.run]) as api:
            with self.assertRaisesRegex(TRANSFORM.SourceRejected, "expired"):
                TRANSFORM.inspect_source(self.repository, None, "456")
            self.assertEqual(api.call_count, 2)

    def test_explicit_run_id_still_checks_artifact_ownership(self):
        wrong = dict(self.artifact, workflow_run={"id": 999})
        with mock.patch.object(TRANSFORM, "api_json", side_effect=[self.run, wrong]):
            with self.assertRaisesRegex(TRANSFORM.SourceRejected, "belong"):
                TRANSFORM.inspect_source(self.repository, "123", "456")

    def test_invalid_artifact_id_is_rejected_before_any_api_call(self):
        with mock.patch.object(TRANSFORM, "api_json") as api:
            for value in ("0", "../456", "", "456?x=y"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    TRANSFORM.inspect_source(self.repository, None, value)
            api.assert_not_called()

    def test_catalog_is_sorted_and_reuses_successful_attempt_checks(self):
        newer = dict(self.artifact, id=457, created_at="2026-10-28T11:00:00Z")
        catalog = self.query([[self.artifact, newer]])
        self.assertEqual([item['artifact_id'] for item in catalog['candidates']], [457, 456])
        self.assertTrue(catalog['scan_complete'])
        self.assertEqual(catalog['scanned_artifacts'], 2)
        self.assertEqual(len(self.calls), 3)  # Listing, producing run, selected attempt.
        rendered = TRANSFORM.render_inputs(catalog)
        self.assertIn("只填写选中行的 Artifact ID", rendered)
        self.assertIn("2026-10-28T11:00:00Z", rendered)
        self.assertIn("/actions/runs/123/artifacts/457", rendered)
        self.assertLess(rendered.index("`457`"), rendered.index("`456`"))

    def test_current_attempt_does_not_require_an_extra_attempt_lookup(self):
        artifact = dict(self.artifact, name="rootfs-trixie-amd64-vm-incusd-123-2")
        catalog = self.query([[artifact]], attempts={})
        self.assertEqual(catalog['candidates'][0]['run_attempt'], 2)
        self.assertEqual(len(self.calls), 2)

    def test_both_registered_base_targets_are_listed(self):
        memini = dict(self.artifact, id=457, name="rootfs-trixie-amd64-memini-123-1")
        catalog = self.query([[self.artifact, memini]])
        self.assertEqual({item['target'] for item in catalog['candidates']},
                         {'amd64-trixie-vm-incusd', 'amd64-trixie-memini'})

    def test_logs_transforms_removed_names_and_expired_inputs_are_filtered_before_run_queries(self):
        entries = [dict(self.artifact, id=500+i, name=name) for i, name in enumerate((
            "logs-rootfs-trixie-amd64-vm-incusd-123-1",
            "rootfs-trixie-amd64-vm-incusd-br-initrd-123-1",
            "rootfs-trixie-amd64-removed-device-123-1"))]
        entries += [dict(self.artifact, id=510, expired=True),
                    dict(self.artifact, id=511, expires_at="2026-10-28T11:59:59Z"),
                    dict(self.artifact, id=512, workflow_run=None)]
        catalog = self.query([entries], runs={})
        self.assertEqual(catalog['candidates'], [])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("没有可用基础产物", TRANSFORM.render_inputs(catalog))

    def test_unsuccessful_foreign_and_wrong_workflow_runs_are_not_candidates(self):
        for changes in ({"conclusion": "failure"}, {"status": "in_progress"},
                        {"head_repository": {"full_name": "other/fork"}},
                        {"path": ".github/workflows/rootfs-transform.yml"}):
            with self.subTest(changes=changes):
                catalog = self.query([[self.artifact]], runs={123: dict(self.run, **changes)})
                self.assertEqual(catalog['candidates'], [])

    def test_a_failed_earlier_attempt_is_not_accepted_after_a_successful_rerun(self):
        catalog = self.query([[self.artifact]], attempts={(123, 1): dict(self.run, run_attempt=1, conclusion="failure")})
        self.assertEqual(catalog['candidates'], [])

    def test_unsupported_architecture_has_no_catalog_fallback(self):
        with mock.patch.dict(TRANSFORM.BACKENDS, {}, clear=True):
            self.assertEqual(self.query([[self.artifact]])['candidates'], [])

    def test_pagination_includes_later_pages_and_deduplicates_overlapping_entries(self):
        logs = [dict(self.artifact, id=1000+i, name="logs-unrelated") for i in range(99)]
        newer = dict(self.artifact, id=457, created_at="2026-10-28T11:00:00Z")
        catalog = self.query([[self.artifact, *logs], [self.artifact, newer]])
        self.assertEqual([item['artifact_id'] for item in catalog['candidates']], [457, 456])
        self.assertEqual([query for path, query in self.calls if query is not None],
                         [{'per_page': 100, 'page': 1}, {'per_page': 100, 'page': 2}])

    def test_scan_limit_is_explicit_in_the_summary(self):
        logs = [dict(self.artifact, id=1000+i, name="logs-unrelated") for i in range(100)]
        catalog = self.query([logs, logs], CATALOG_MAX_PAGES=2)
        self.assertFalse(catalog['scan_complete'])
        self.assertEqual(catalog['scanned_artifacts'], 200)
        self.assertIn("扫描上限", TRANSFORM.render_inputs(catalog))

    def test_at_most_twenty_newest_eligible_artifacts_are_displayed(self):
        artifacts = [dict(self.artifact, id=500+i) for i in range(25)]
        catalog = self.query([artifacts])
        self.assertEqual(len(catalog['candidates']), 20)
        self.assertEqual([item['artifact_id'] for item in catalog['candidates']], list(range(524, 504, -1)))

    def test_api_errors_are_not_reported_as_an_empty_catalog(self):
        for failures in ([ValueError("GitHub request failed (HTTP 403)")],
                         [{"artifacts": [self.artifact]}, ValueError("GitHub request failed (HTTP 404)")],
                         [{"artifacts": [self.artifact]}, self.run, ValueError("GitHub request failed (HTTP 429)")]):
            with self.subTest(failures=failures), mock.patch.object(TRANSFORM, "api_json", side_effect=failures):
                with self.assertRaisesRegex(ValueError, "GitHub request failed"):
                    TRANSFORM.list_inputs(self.repository, self.now)

    def test_malformed_listing_and_timestamps_fail_instead_of_hiding_a_query_error(self):
        for response in ({"message": "not a listing"}, {"artifacts": [dict(self.artifact, created_at="unknown")]}):
            with self.subTest(response=response), mock.patch.object(TRANSFORM, "api_json", return_value=response):
                with self.assertRaises(ValueError):
                    TRANSFORM.list_inputs(self.repository, self.now)

    def test_api_pagination_uses_a_fixed_host_and_controlled_parameters(self):
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(b'{}')
        with mock.patch.dict(os.environ, GH_TOKEN="fixture-token"), \
                mock.patch.object(urllib.request, "build_opener", return_value=opener):
            with TRANSFORM.github_request(f"/repos/{self.repository}/actions/artifacts",
                                          query={"per_page": 100, "page": 2}):
                pass
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url,
                         f"https://api.github.com/repos/{self.repository}/actions/artifacts?per_page=100&page=2")
        with mock.patch.dict(os.environ, GH_TOKEN="fixture-token"), \
                mock.patch.object(urllib.request, "build_opener") as network:
            for path, query in (("https://other.example/artifacts", None),
                                (f"/repos/{self.repository}/actions/artifacts?per_page=100", None),
                                (f"/repos/{self.repository}/actions/artifacts", {'page': 1, 'per_page': 100, 'url': 'evil'}),
                                (f"/repos/{self.repository}/actions/runs/123", {'page': 1, 'per_page': 100}),
                                (f"/repos/{self.repository}/actions/artifacts", {'page': 0, 'per_page': 100})):
                with self.subTest(path=path, query=query), self.assertRaises(ValueError):
                    TRANSFORM.github_request(path, query=query)
            network.assert_not_called()

    def test_cli_inspect_without_run_id_and_catalog_output(self):
        temporary = tempfile.TemporaryDirectory(prefix="rootfs-catalog-cli-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        output, summary = root/'source.json', root/'summary.md'
        args = ['transform.py', 'inspect', '--repository', self.repository, '--artifact-id', '456', '--output', str(output)]
        with mock.patch.object(sys, 'argv', args), \
                mock.patch.object(TRANSFORM, 'api_json', side_effect=[self.artifact, self.run, dict(self.run, run_attempt=1)]):
            TRANSFORM.main()
        self.assertEqual(json.loads(output.read_text())['run_id'], 123)
        args = ['transform.py', 'list-inputs', '--repository', self.repository, '--summary', str(summary)]
        with mock.patch.object(sys, 'argv', args), redirect_stdout(io.StringIO()), \
                mock.patch.object(TRANSFORM, 'api_json', side_effect=[{'artifacts': [self.artifact]}, self.run,
                                                                     dict(self.run, run_attempt=1)]):
            TRANSFORM.main()
        self.assertIn('`456`', summary.read_text())

    def test_cli_query_failure_does_not_write_an_empty_success_summary(self):
        temporary = tempfile.TemporaryDirectory(prefix="rootfs-catalog-error-")
        self.addCleanup(temporary.cleanup)
        summary = Path(temporary.name)/'summary.md'
        args = ['transform.py', 'list-inputs', '--repository', self.repository, '--summary', str(summary)]
        error_log = io.StringIO()
        with mock.patch.object(sys, 'argv', args), redirect_stderr(error_log), \
                mock.patch.object(TRANSFORM, 'api_json', side_effect=ValueError('GitHub request failed (HTTP 403)')):
            with self.assertRaises(SystemExit) as exit_status:
                TRANSFORM.main()
        self.assertEqual(exit_status.exception.code, 1)
        self.assertIn('HTTP 403', error_log.getvalue())
        self.assertFalse(summary.exists())


if __name__ == "__main__":
    unittest.main()

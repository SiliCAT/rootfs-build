"""Exercise the receipt CLI with actual metadata files and upload-action outputs."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/artifact-reference.py"


class ArtifactReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rootfs-reference-tests-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.output = self.base / "output"
        self.output.mkdir()
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.receipt = self.logs / "artifact-reference.json"
        self.summary = self.base / "summary.md"
        self.summary.write_text("Earlier summary content\n\n")
        self.repository = "SiliCAT/rootfs-build"
        self.run_id, self.artifact_id = 2000007, 9000009
        self.run_url = f"https://github.com/{self.repository}/actions/runs/{self.run_id}"
        self.artifact_url = f"{self.run_url}/artifacts/{self.artifact_id}"
        self.environment = dict(os.environ, GITHUB_SERVER_URL="https://github.com",
            GITHUB_REPOSITORY=self.repository, GITHUB_SHA="b" * 40,
            GITHUB_RUN_ID=str(self.run_id), GITHUB_RUN_ATTEMPT="2",
            ROOTFS_ARTIFACT_ID=str(self.artifact_id), ROOTFS_ARTIFACT_URL=self.artifact_url,
            ROOTFS_ARTIFACT_DIGEST="c" * 64, GH_TOKEN="fixture-secret-must-not-be-recorded")
        self.metadata = {
            "target": {"id": "amd64-trixie-vm-incusd", "artifact_name": "rootfs-trixie-amd64-vm-incusd"},
            "source_repository": self.repository, "source_commit": "b" * 40,
            "workflow_run_url": self.run_url,
            "archive": "rootfs-trixie-amd64-vm-incusd.tar.zst",
            "archive_sha256": hashlib.sha256(b"fixture rootfs archive").hexdigest(),
        }

    def transform_metadata(self, optional_digest=False):
        base = copy.deepcopy(self.metadata)
        source_run, source_artifact = 1000001, 8000003
        source_url = f"https://github.com/{self.repository}/actions/runs/{source_run}"
        base.update(source_commit="a" * 40, workflow_run_url=source_url)
        value = copy.deepcopy(self.metadata)
        value["target"]["id"] += "-br-initrd"
        value["target"]["artifact_name"] += "-br-initrd"
        value["archive"] = value["target"]["artifact_name"] + ".tar.zst"
        value["transform"] = {"transform": "br-initrd"}
        value["base_build"] = base
        value["input"] = {
            "repository": self.repository, "run_id": source_run, "run_attempt": 1,
            "run_url": source_url, "artifact_id": source_artifact,
            "artifact_name": base["target"]["artifact_name"] + f"-{source_run}-1",
            "artifact_digest": None if optional_digest else "sha256:" + "d" * 64,
            "archive": base["archive"], "archive_sha256": base["archive_sha256"],
        }
        return value

    def run_cli(self, metadata=None, kind="base", environment=None):
        metadata = self.metadata if metadata is None else metadata
        self.metadata_path = self.output / (metadata["target"]["artifact_name"] + ".build.json")
        self.metadata_path.write_text(json.dumps(metadata))
        return subprocess.run([sys.executable, str(SCRIPT), "--kind", kind,
                               "--metadata", str(self.metadata_path), "--output", str(self.receipt),
                               "--summary", str(self.summary)],
                              env=self.environment if environment is None else environment,
                              capture_output=True, text=True, timeout=5)

    def assert_no_reference(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.summary.read_text(), "Earlier summary content\n\n")
        self.assertNotIn(self.environment["GH_TOKEN"], result.stdout + result.stderr)

    def test_base_receipt_and_copyable_summary_match_without_modifying_uploaded_files(self):
        stem = self.metadata["target"]["artifact_name"]
        self.metadata_path = self.output / (stem + ".build.json")
        self.metadata_path.write_text(json.dumps(self.metadata))
        for suffix in (".tar.zst", ".tar.zst.sha256", ".packages.tsv", ".SHA256SUMS"):
            (self.output / (stem + suffix)).write_bytes(b"immutable uploaded fixture")
        before = {path.name: path.read_bytes() for path in self.output.iterdir()}
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reference = json.loads(self.receipt.read_text())
        summary = self.summary.read_text()
        self.assertEqual(reference["schema_version"], 1)
        self.assertEqual(reference["kind"], "base")
        self.assertEqual(reference["target"], self.metadata["target"]["id"])
        self.assertEqual(reference["run_id"], self.run_id)
        self.assertEqual(reference["run_attempt"], 2)
        self.assertEqual(reference["artifact_id"], self.artifact_id)
        self.assertEqual(reference["artifact_name"], f"{stem}-{self.run_id}-2")
        self.assertEqual(reference["artifact_url"], self.artifact_url)
        self.assertEqual(reference["artifact_digest"], "sha256:" + "c" * 64)
        self.assertEqual(reference["archive_sha256"], self.metadata["archive_sha256"])
        self.assertNotEqual(reference["artifact_digest"].removeprefix("sha256:"), reference["archive_sha256"])
        self.assertIn(f"source_run_id:      {reference['run_id']}", summary)
        self.assertIn(f"source_artifact_id: {reference['artifact_id']}", summary)
        self.assertIn(reference["artifact_url"], summary)
        self.assertIn(reference["archive_sha256"], summary)
        self.assertTrue(summary.startswith("Earlier summary content\n\n"))
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o644)
        self.assertNotIn("input", reference)
        self.assertNotIn(self.environment["GH_TOKEN"], self.receipt.read_text() + summary + result.stdout)
        self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, before)

    def test_transform_receipt_separates_source_and_output_ids(self):
        metadata = self.transform_metadata()
        result = self.run_cli(metadata, kind="transform")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reference = json.loads(self.receipt.read_text())
        self.assertEqual(reference["kind"], "transform")
        self.assertEqual(reference["target"], "amd64-trixie-vm-incusd-br-initrd")
        self.assertEqual(reference["artifact_id"], self.artifact_id)
        self.assertEqual(reference["input"]["artifact_id"], metadata["input"]["artifact_id"])
        self.assertEqual(reference["input"]["run_id"], metadata["input"]["run_id"])
        self.assertEqual(reference["input"]["source_commit"], "a" * 40)
        self.assertEqual(reference["source_commit"], "b" * 40)
        self.assertEqual(reference["input"]["artifact_url"],
                         metadata["input"]["run_url"] + "/artifacts/8000003")
        summary = self.summary.read_text()
        self.assertIn("本次加工输出", summary)
        self.assertIn("基础输入（来源）", summary)
        self.assertIn(str(self.artifact_id), summary)
        self.assertIn(str(metadata["input"]["artifact_id"]), summary)
        self.assertNotIn("source_artifact_id:", summary)
        self.assertIn("不能作为该入口的基础输入", summary)

    def test_missing_source_artifact_digest_is_preserved_without_fabricating_one(self):
        result = self.run_cli(self.transform_metadata(optional_digest=True), kind="transform")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(json.loads(self.receipt.read_text())["input"]["artifact_digest"])
        self.assertIn("未提供", self.summary.read_text())

    def test_prefixed_action_digest_is_also_accepted(self):
        result = self.run_cli(environment=dict(self.environment, ROOTFS_ARTIFACT_DIGEST="sha256:" + "c" * 64))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.receipt.read_text())["artifact_digest"], "sha256:" + "c" * 64)

    def test_missing_or_invalid_upload_outputs_never_produce_receipts(self):
        for key, value in (("ROOTFS_ARTIFACT_ID", None), ("ROOTFS_ARTIFACT_ID", ""),
                           ("ROOTFS_ARTIFACT_ID", "0"), ("ROOTFS_ARTIFACT_ID", "not-an-id"),
                           ("ROOTFS_ARTIFACT_DIGEST", ""), ("ROOTFS_ARTIFACT_DIGEST", "bad")):
            with self.subTest(key=key, value=value):
                environment = dict(self.environment)
                if value is None:
                    environment.pop(key)
                else:
                    environment[key] = value
                self.assert_no_reference(self.run_cli(environment=environment))

    def test_upload_url_must_match_the_main_artifact_run_and_repository(self):
        for url in (self.artifact_url.replace("2000007", "2000008"),
                    self.artifact_url.replace("9000009", "9000010"),
                    self.artifact_url.replace("SiliCAT", "other"),
                    self.artifact_url.replace("https:", "http:")):
            with self.subTest(url=url):
                self.assert_no_reference(self.run_cli(environment=dict(self.environment, ROOTFS_ARTIFACT_URL=url)))

    def test_metadata_from_another_build_is_rejected(self):
        for changes in ({"source_commit": "a" * 40}, {"source_repository": "other/repo"},
                        {"workflow_run_url": self.run_url + "0"}, {"archive": "wrong.tar.zst"},
                        {"archive_sha256": "invalid"}):
            with self.subTest(changes=changes):
                self.assert_no_reference(self.run_cli(dict(self.metadata, **changes)))

    def test_transform_cannot_offer_its_output_as_a_base_input(self):
        self.assert_no_reference(self.run_cli(self.transform_metadata(), kind="base"))

    def test_base_metadata_cannot_be_reported_as_a_transform(self):
        self.assert_no_reference(self.run_cli(kind="transform"))

    def test_transform_source_must_agree_with_original_base_metadata(self):
        for changes in ({"run_id": 1000002}, {"archive_sha256": "e" * 64},
                        {"artifact_name": "logs-rootfs-fixture"}, {"artifact_id": False}):
            with self.subTest(changes=changes):
                metadata = self.transform_metadata()
                metadata["input"].update(changes)
                self.assert_no_reference(self.run_cli(metadata, kind="transform"))

    def test_existing_receipt_and_summary_are_not_overwritten_on_duplicate_publish(self):
        self.assertEqual(self.run_cli().returncode, 0)
        receipt, summary = self.receipt.read_bytes(), self.summary.read_bytes()
        result = self.run_cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.receipt.read_bytes(), receipt)
        self.assertEqual(self.summary.read_bytes(), summary)

    def test_metadata_cannot_inject_markdown_into_the_summary(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["target"]["id"] = "target`\n# forged content"
        self.assert_no_reference(self.run_cli(metadata))


if __name__ == "__main__":
    unittest.main()

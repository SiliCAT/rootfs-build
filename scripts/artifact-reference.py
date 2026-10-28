#!/usr/bin/env python3
"""Publish an uploaded rootfs reference to the job summary and diagnostics JSON."""

import argparse
import json
import os
from pathlib import Path
import re


def checked(value, pattern, description):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"invalid {description}")
    return value


def positive_id(value, description):
    if isinstance(value, bool):
        raise ValueError(f"invalid {description}")
    return int(checked(str(value), r"[1-9][0-9]{0,19}", description))


def digest(value, description, optional=False):
    if optional and value is None:
        return None
    value = checked(value, r"(?:sha256:)?[0-9a-f]{64}", description)
    return value if value.startswith("sha256:") else "sha256:" + value


def identity(metadata):
    target = metadata["target"]
    target_id = checked(target["id"], r"[a-z0-9][a-z0-9-]*", "target ID")
    stem = checked(target["artifact_name"], r"rootfs-[a-z0-9][a-z0-9-]*", "artifact stem")
    if metadata["archive"] != stem + ".tar.zst":
        raise ValueError("archive name does not match the target")
    return target_id, stem


def source_reference(metadata, server):
    """Keep the base identity separate from the newly uploaded transform result."""
    source, base = metadata["input"], metadata["base_build"]
    if any(key in base for key in ("input", "base_build", "transform")):
        raise ValueError("transform source must be a base build")
    target_id, stem = identity(base)
    repository = checked(source["repository"], r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "source repository")
    commit = checked(base["source_commit"], r"[0-9a-f]{40}", "base source commit")
    run_id = positive_id(source["run_id"], "source run ID")
    attempt = positive_id(source["run_attempt"], "source run attempt")
    artifact_id = positive_id(source["artifact_id"], "source artifact ID")
    run_url = f"{server}/{repository}/actions/runs/{run_id}"
    artifact_name = f"{stem}-{run_id}-{attempt}"
    archive_hash = checked(source["archive_sha256"], r"[0-9a-f]{64}", "input archive SHA-256")
    if (base["source_repository"].lower() != repository.lower()
            or base["workflow_run_url"] != run_url or source["run_url"] != run_url
            or source["artifact_name"] != artifact_name or source["archive"] != base["archive"]
            or archive_hash != base["archive_sha256"]):
        raise ValueError("base build and transform input references disagree")
    return {
        "repository": repository, "target": target_id, "source_commit": commit,
        "run_id": run_id, "run_attempt": attempt, "run_url": run_url,
        "artifact_id": artifact_id, "artifact_name": artifact_name,
        "artifact_url": f"{run_url}/artifacts/{artifact_id}",
        "artifact_digest": digest(source["artifact_digest"], "source artifact digest", optional=True),
        "archive": source["archive"], "archive_sha256": archive_hash,
    }


def build_reference(kind, metadata, environment):
    if kind not in ("base", "transform"):
        raise ValueError("reference kind must be base or transform")
    target_id, stem = identity(metadata)
    server = checked(environment["GITHUB_SERVER_URL"], r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", "GitHub server URL")
    repository = checked(environment["GITHUB_REPOSITORY"], r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", "repository")
    commit = checked(environment["GITHUB_SHA"], r"[0-9a-f]{40}", "source commit")
    run_id = positive_id(environment["GITHUB_RUN_ID"], "run ID")
    attempt = positive_id(environment["GITHUB_RUN_ATTEMPT"], "run attempt")
    artifact_id = positive_id(environment["ROOTFS_ARTIFACT_ID"], "uploaded artifact ID")
    run_url = f"{server}/{repository}/actions/runs/{run_id}"
    artifact_url = f"{run_url}/artifacts/{artifact_id}"
    if environment["ROOTFS_ARTIFACT_URL"] != artifact_url:
        raise ValueError("upload URL does not match this repository, run and artifact ID")
    if (metadata["source_repository"].lower() != repository.lower()
            or metadata["source_commit"] != commit or metadata["workflow_run_url"] != run_url):
        raise ValueError("build metadata does not match the current workflow run")
    reference = {
        "schema_version": 1, "kind": kind, "repository": repository, "target": target_id,
        "source_commit": commit, "run_id": run_id, "run_attempt": attempt, "run_url": run_url,
        "artifact_id": artifact_id, "artifact_name": f"{stem}-{run_id}-{attempt}",
        "artifact_url": artifact_url,
        "artifact_digest": digest(environment["ROOTFS_ARTIFACT_DIGEST"], "uploaded artifact digest"),
        "archive": metadata["archive"],
        "archive_sha256": checked(metadata["archive_sha256"], r"[0-9a-f]{64}", "archive SHA-256"),
    }
    if kind == "base":
        if any(key in metadata for key in ("input", "base_build", "transform")):
            raise ValueError("a transform result cannot be advertised as a base build")
    else:
        if not isinstance(metadata.get("transform"), dict):
            raise ValueError("transform metadata is required for a transform reference")
        reference["input"] = source_reference(metadata, server)
    return reference


def reference_table(reference):
    rows = [
        ("目标", f"`{reference['target']}`"),
        ("仓库", f"`{reference['repository']}`"),
        ("源码提交", f"`{reference['source_commit']}`"),
        ("Run ID", f"`{reference['run_id']}`"),
        ("运行尝试", f"`{reference['run_attempt']}`"),
        ("主 rootfs Artifact ID", f"`{reference['artifact_id']}`"),
        ("产物名称", f"`{reference['artifact_name']}`"),
        ("运行链接", f"[查看运行]({reference['run_url']})"),
        ("主产物下载", f"[下载 rootfs 产物]({reference['artifact_url']})"),
        ("rootfs 归档", f"`{reference['archive']}`"),
        ("归档 SHA-256", f"`{reference['archive_sha256']}`"),
        ("Actions 产物 digest", f"`{reference['artifact_digest']}`" if reference['artifact_digest'] else "未提供"),
    ]
    return "| 项目 | 内容 |\n| --- | --- |\n" + "".join(f"| {key} | {value} |\n" for key, value in rows)


def render_summary(reference):
    if reference["kind"] == "base":
        return (
            "## 基础 rootfs 产物\n\n主 rootfs 产物已上传。\n\n"
            "### 供 Transform rootfs 使用\n\n"
            f"```text\nsource_run_id:      {reference['run_id']}\n"
            f"source_artifact_id: {reference['artifact_id']}\n```\n\n"
            + reference_table(reference)
        )
    return (
        "## br-initrd 加工产物\n\n主 rootfs 产物已上传。\n\n"
        "### 本次加工输出\n\n" + reference_table(reference)
        + "\n### 基础输入（来源）\n\n" + reference_table(reference["input"])
        + "\n当前 Transform rootfs 入口只接受成功的基础构建产物；"
          "本次加工输出不能作为该入口的基础输入。\n"
    )


def publish_reference(kind, metadata_path, output, summary, environment):
    metadata = json.loads(metadata_path.read_text())
    reference = build_reference(kind, metadata, environment)
    rendered = render_summary(reference)
    # Do not modify the uploaded files or replace an earlier receipt. Diagnostics
    # are uploaded afterwards; this file describes the main rootfs artifact only.
    with output.open("x") as stream:
        json.dump(reference, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        os.fchmod(stream.fileno(), 0o644)
    with summary.open("a") as stream:
        stream.write(rendered + "\n")
    return reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("base", "transform"), required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args()
    try:
        reference = publish_reference(args.kind, args.metadata, args.output, args.summary, os.environ)
        print(f"Rootfs reference ready: run {reference['run_id']}, artifact {reference['artifact_id']}")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        parser.exit(1, f"artifact reference: {error}\n")


if __name__ == "__main__":
    main()

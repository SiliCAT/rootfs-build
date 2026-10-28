#!/usr/bin/env python3
"""Resolve, download and verify a base Actions artifact; export transform provenance."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import target

# Execution support is explicit and separate from the shared business recipe.
# Add a backend only after native builds have been validated on that architecture.
BACKENDS = {
    "amd64": {"runner": "ubuntu-24.04", "platform": "linux/amd64",
              "builder_image": "debian:trixie-slim@sha256:89c1ea91e897f0fe0a3731595330bc45b8bdef4047464a07116f891a94054b31"},
}
RECIPE = "transforms/trixie/br-initrd/recipe.sh"
MAX_ARTIFACT_BYTES = 8 * 1024**3
CATALOG_PAGE_SIZE = 100
CATALOG_MAX_PAGES = 10
CATALOG_LIMIT = 20


class SourceRejected(ValueError):
    """An artifact is not an eligible base input; API failures remain fatal."""


def checked_repository(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("expected an OWNER/REPOSITORY name")
    return repository


def api_time(value):
    if not isinstance(value, str):
        raise ValueError("invalid artifact timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid artifact timestamp") from None
    if result.tzinfo is None:
        raise ValueError("artifact timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def positive_id(value):
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise ValueError("run and artifact IDs must be positive decimal integers")
    return int(value)


def backend(architecture):
    if architecture not in BACKENDS:
        raise ValueError(f"no validated native transform backend for architecture: {architecture}")
    return dict(BACKENDS[architecture])


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("artifact redirects must use HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def github_request(path, *, query=None):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("GH_TOKEN is required for Actions artifact access")
    pattern = (r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/"
               r"(?:runs/[1-9][0-9]*(?:/attempts/[1-9][0-9]*)?|"
               r"artifacts(?:/[1-9][0-9]*(?:/zip)?)?)")
    if not re.fullmatch(pattern, path):
        raise ValueError("invalid GitHub API path")
    suffix = ""
    if query is not None:
        if (not path.endswith("/actions/artifacts") or set(query) != {"page", "per_page"}
                or type(query["page"]) is not int or not 1 <= query["page"] <= CATALOG_MAX_PAGES
                or type(query["per_page"]) is not int or query["per_page"] != CATALOG_PAGE_SIZE):
            raise ValueError("invalid artifact pagination parameters")
        suffix = "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request("https://api.github.com" + path + suffix, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "rootfs-build-transform",
    })
    try:
        return urllib.request.build_opener(SafeRedirect()).open(request, timeout=60)
    except urllib.error.HTTPError as error:
        raise ValueError(f"GitHub request failed (HTTP {error.code})") from None
    except urllib.error.URLError:
        raise ValueError("GitHub request failed: network or TLS error") from None


def api_json(path, *, query=None):
    with github_request(path, query=query) as response:
        return json.load(response)


def registered_bases():
    registry = json.loads((target.REPOSITORY / "targets/targets.json").read_text())
    return [target.resolve(target_id) for target_id in registry]


def artifact_run_id(artifact):
    try:
        return positive_id((artifact.get("workflow_run") or {})["id"])
    except (ValueError, KeyError, TypeError):
        raise SourceRejected("artifact has no valid producing workflow run") from None


def artifact_matches(artifact, run_id, bases):
    matches = []
    name = artifact.get("name", "")
    for base in bases:
        prefix = f"{base['artifact_name']}-{run_id}-"
        if name.startswith(prefix) and re.fullmatch(r"[1-9][0-9]*", name[len(prefix):]):
            matches.append((base, int(name[len(prefix):])))
    return matches


def cached_json(path, cache):
    if cache is None:
        return api_json(path)
    if path not in cache:
        cache[path] = api_json(path)
    return cache[path]


def inspect_source(repository, run_id, artifact_id):
    checked_repository(repository)
    artifact_id = positive_id(artifact_id)
    if run_id is None:
        artifact = api_json(f"/repos/{repository}/actions/artifacts/{artifact_id}")
        run_id = artifact_run_id(artifact)
        run = api_json(f"/repos/{repository}/actions/runs/{run_id}")
    else:
        # Retain explicit run-ID checks for saved descriptors and local callers.
        run_id = positive_id(run_id)
        run = api_json(f"/repos/{repository}/actions/runs/{run_id}")
        artifact = api_json(f"/repos/{repository}/actions/artifacts/{artifact_id}")
    return validate_source(repository, run_id, artifact_id, run, artifact, registered_bases())


def validate_source(repository, run_id, artifact_id, run, artifact, bases, cache=None, now=None):
    if (run.get("id") != run_id or run.get("status") != "completed"
            or run.get("conclusion") != "success"):
        raise SourceRejected("source run must be completed and successful")
    for key in ("repository", "head_repository"):
        if (run.get(key) or {}).get("full_name", "").lower() != repository.lower():
            raise SourceRejected("source run must belong to this repository")
    if run.get("path", "").split("@", 1)[0] != ".github/workflows/rootfs.yml":
        raise SourceRejected("source must be produced by the base rootfs workflow")
    if (artifact.get("id") != artifact_id or artifact.get("expired") is not False
            or (artifact.get("workflow_run") or {}).get("id") != run_id):
        raise SourceRejected("artifact must be unexpired and belong to the selected run")
    if (artifact.get("expires_at") is not None
            and api_time(artifact["expires_at"]) <= (now or datetime.now(timezone.utc))):
        raise SourceRejected("source artifact has expired")
    commit = run.get("head_sha", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SourceRejected("invalid source commit")
    matches = artifact_matches(artifact, run_id, bases)
    if len(matches) != 1 or matches[0][1] > run.get("run_attempt", 0):
        raise SourceRejected("artifact name must identify exactly one registered base target and run attempt")
    base, attempt = matches[0]
    selected_run = (cached_json(f"/repos/{repository}/actions/runs/{run_id}/attempts/{attempt}", cache)
                    if attempt != run["run_attempt"] else run)
    if (selected_run.get("id") != run_id or selected_run.get("run_attempt") != attempt
            or selected_run.get("status") != "completed" or selected_run.get("conclusion") != "success"
            or selected_run.get("head_sha") != commit):
        raise SourceRejected("the selected artifact's run attempt must also be successful")
    if base["suite"] != "trixie":
        raise SourceRejected("br-initrd currently requires Debian Trixie")
    if base["architecture"] not in BACKENDS:
        raise SourceRejected("input architecture has no validated native backend")
    execution = backend(base["architecture"])
    digest = artifact.get("digest")
    if digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SourceRejected("invalid artifact digest")
    return {"repository": repository, "run_id": run_id, "run_attempt": attempt,
            "artifact_id": artifact_id, "artifact_name": artifact["name"],
            "artifact_digest": digest, "source_commit": commit,
            "run_url": f"https://github.com/{repository}/actions/runs/{run_id}",
            "base_target": base, "backend": execution}


def list_inputs(repository, now=None):
    checked_repository(repository)
    now = now or datetime.now(timezone.utc)
    bases, artifacts, cache = registered_bases(), {}, {}
    scanned, complete = 0, False
    for page in range(1, CATALOG_MAX_PAGES + 1):
        result = api_json(f"/repos/{repository}/actions/artifacts",
                          query={"per_page": CATALOG_PAGE_SIZE, "page": page})
        entries = result.get("artifacts")
        if not isinstance(entries, list) or len(entries) > CATALOG_PAGE_SIZE:
            raise ValueError("invalid artifact listing response")
        scanned += len(entries)
        for artifact in entries:
            if artifact.get("expired") is not False:
                continue
            try:
                run_id = artifact_run_id(artifact)
                artifact_id = positive_id(artifact.get("id"))
            except SourceRejected:
                continue
            if not artifact_matches(artifact, run_id, bases):
                continue
            if api_time(artifact.get("expires_at")) <= now:
                continue
            created = api_time(artifact.get("created_at"))
            artifacts[artifact_id] = (created, artifact)
        if len(entries) < CATALOG_PAGE_SIZE:
            complete = True
            break
    ordered = sorted(artifacts.values(), key=lambda item: (item[0], item[1]["id"]), reverse=True)
    candidates = []
    for created, artifact in ordered:
        run_id, artifact_id = artifact_run_id(artifact), positive_id(artifact["id"])
        run = cached_json(f"/repos/{repository}/actions/runs/{run_id}", cache)
        try:
            source = validate_source(repository, run_id, artifact_id, run, artifact, bases, cache, now)
        except SourceRejected:
            continue
        candidates.append({"target": source["base_target"]["id"],
            "created_at": created.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "source_commit": source["source_commit"], "run_id": run_id,
            "run_attempt": source["run_attempt"], "artifact_id": artifact_id,
            "artifact_name": source["artifact_name"], "run_url": source["run_url"],
            "artifact_url": f"{source['run_url']}/artifacts/{artifact_id}"})
        if len(candidates) == CATALOG_LIMIT:
            break
    return {"repository": repository, "scanned_artifacts": scanned,
            "scan_complete": complete, "candidates": candidates}


def render_inputs(catalog):
    lines = ["## 可用基础 rootfs 候选", "",
             f"仓库：`{catalog['repository']}`。已扫描 {catalog['scanned_artifacts']} 条产物元数据，"
             f"最多显示按创建时间从新到旧排序的 {CATALOG_LIMIT} 个候选。", ""]
    if not catalog["scan_complete"]:
        lines += [f"已达到 {CATALOG_MAX_PAGES} 页扫描上限；清单仅覆盖已扫描范围。", ""]
    if not catalog["candidates"]:
        lines += ["没有可用基础产物。", ""]
    else:
        lines += ["在 **Transform rootfs → Run workflow** 中只填写选中行的 Artifact ID。", "",
                  "| 目标 | 创建时间（UTC） | 源码提交 | Run ID | 尝试 | Artifact ID | 链接 |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for item in catalog["candidates"]:
            lines.append(f"| `{item['target']}` | {item['created_at']} | `{item['source_commit'][:12]}` | "
                         f"`{item['run_id']}` | {item['run_attempt']} | `{item['artifact_id']}` | "
                         f"[运行]({item['run_url']}) · [下载]({item['artifact_url']}) |")
        lines += ["", "候选基于查询时的 API 元数据；启动加工时会重新检查来源和有效期，"
                  "下载后继续校验归档及构建记录。", ""]
    return "\n".join(lines)


def expected_files(source):
    stem = source["base_target"]["artifact_name"]
    return {f"{stem}{suffix}" for suffix in (
        ".tar.zst", ".tar.zst.sha256", ".packages.tsv", ".build.json", ".SHA256SUMS")}


def unpack_artifact(archive, directory, source):
    expected = expected_files(source)
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if (len(entries) != len(expected) or {entry.filename for entry in entries} != expected
                or sum(entry.file_size for entry in entries) > MAX_ARTIFACT_BYTES):
            raise ValueError("artifact must contain exactly the five expected base build files")
        for entry in entries:
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError("artifact entries must be regular files")
        for entry in entries:
            with bundle.open(entry) as reader, (directory / entry.filename).open("xb") as writer:
                shutil.copyfileobj(reader, writer)


def read_sums(path):
    sums = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-z0-9][a-zA-Z0-9._-]*)", line)
        if not match or match[2] in sums:
            raise ValueError("invalid or duplicate checksum entry")
        sums[match[2]] = match[1]
    return sums


def verify_input(directory, source):
    base = source["base_target"]
    backend(base["architecture"])
    if base["suite"] != "trixie":
        raise ValueError("input must be Debian Trixie")
    stem = base["artifact_name"]
    expected = expected_files(source)
    for name in expected:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular input file: {name}")
    sums = read_sums(directory / f"{stem}.SHA256SUMS")
    if set(sums) != expected - {f"{stem}.SHA256SUMS"}:
        raise ValueError("SHA256SUMS does not cover the exact base build files")
    for name, digest in sums.items():
        if target.sha256(directory / name) != digest:
            raise ValueError(f"input checksum mismatch: {name}")
    archive_name = f"{stem}.tar.zst"
    if read_sums(directory / f"{archive_name}.sha256") != {archive_name: sums[archive_name]}:
        raise ValueError("archive checksum files disagree")
    build = json.loads((directory / f"{stem}.build.json").read_text())
    identity = build.get("target", {})
    if any(identity.get(key) != base[key] for key in ("id", "architecture", "suite", "name", "artifact_name")):
        raise ValueError("base build identity does not match the selected artifact")
    if (build.get("source_repository", "").lower() != source["repository"].lower()
            or build.get("source_commit") != source["source_commit"]
            or build.get("workflow_run_url") != source["run_url"]
            or build.get("archive") != archive_name
            or build.get("archive_sha256") != sums[archive_name]):
        raise ValueError("base build provenance does not match the selected run and archive")
    return {"source": source, "base_build": build, "archive": archive_name,
            "archive_sha256": sums[archive_name]}


def fetch_source(source, directory):
    # Revalidate after job scheduling; do not use a stale or substituted descriptor.
    fresh = inspect_source(source["repository"], source["run_id"], source["artifact_id"])
    if fresh != source:
        raise ValueError("source descriptor changed since resolution")
    directory.mkdir(parents=True, exist_ok=False)
    archive = directory / "artifact.zip.part"
    path = f"/repos/{source['repository']}/actions/artifacts/{source['artifact_id']}/zip"
    with github_request(path) as response, archive.open("xb") as writer:
        size = 0
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > MAX_ARTIFACT_BYTES:
                raise ValueError("artifact exceeds input size limit")
            writer.write(block)
    if source["artifact_digest"] and "sha256:" + target.sha256(archive) != source["artifact_digest"]:
        raise ValueError("downloaded artifact digest mismatch")
    unpack_artifact(archive, directory, source)
    verified = verify_input(directory, source)
    (directory / "source.json").write_text(json.dumps(source, indent=2) + "\n")
    archive.unlink()
    return verified


def derived_target(source):
    base = source["base_target"]
    execution = backend(base["architecture"])
    return dict(base, id=base["id"] + "-br-initrd", name=base["name"] + "-br-initrd",
                recipe=RECIPE, artifact_name=base["artifact_name"] + "-br-initrd", **execution)


def export_transform(directory, root, output, report_path):
    source = json.loads((directory / "source.json").read_text())
    verified = verify_input(directory, source)
    report = json.loads(report_path.read_text())
    if (report.get("transform") != "br-initrd" or report.get("selection") != "update-initramfs -u"
            or report.get("boot_tested") is not False or not report.get("accepted_initramfs")):
        raise ValueError("missing final initramfs acceptance report")
    seen = set()
    for image in report["accepted_initramfs"]:
        path, kernel = image.get("path", ""), image.get("kernel", "")
        if (not re.fullmatch(r"[a-zA-Z0-9._+~-]+", kernel)
                or path != f"/boot/initrd.img-{kernel}" or path in seen):
            raise ValueError("invalid or duplicate accepted initramfs")
        seen.add(path)
        candidate = (root / path.lstrip("/")).resolve(strict=True)
        if not candidate.is_relative_to(root.resolve()) or target.sha256(candidate) != image["sha256"]:
            raise ValueError("accepted initramfs changed after verification")
    target.export_rootfs_metadata(derived_target(source), root, output, extra={
        "transform": report, "input": {
            "repository": source["repository"], "run_id": source["run_id"],
            "run_attempt": source["run_attempt"], "artifact_id": source["artifact_id"],
            "artifact_name": source["artifact_name"], "artifact_digest": source["artifact_digest"],
            "run_url": source["run_url"], "archive": verified["archive"],
            "archive_sha256": verified["archive_sha256"],
        }, "base_build": verified["base_build"],
    })


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--repository", required=True)
    inspect.add_argument("--run-id", help="Optional producing run ID; inferred from the artifact when omitted")
    inspect.add_argument("--artifact-id", required=True)
    inspect.add_argument("--output", required=True, type=Path)
    inspect.add_argument("--github-output", type=Path)
    listing = sub.add_parser("list-inputs")
    listing.add_argument("--repository", required=True)
    listing.add_argument("--summary", required=True, type=Path)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--source", required=True, type=Path)
    fetch.add_argument("--directory", required=True, type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--directory", required=True, type=Path)
    verify.add_argument("--output", required=True, type=Path)
    export = sub.add_parser("export")
    export.add_argument("--directory", required=True, type=Path)
    export.add_argument("--root", required=True, type=Path)
    export.add_argument("--output", required=True, type=Path)
    export.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            source = inspect_source(args.repository, args.run_id, args.artifact_id)
            write_json(args.output, source)
            if args.github_output:
                values = dict(source["backend"], artifact_name=derived_target(source)["artifact_name"],
                              source_json=json.dumps(source, separators=(",", ":")))
                with args.github_output.open("a") as stream:
                    for key, value in values.items():
                        stream.write(f"{key}={value}\n")
        elif args.command == "list-inputs":
            catalog = list_inputs(args.repository)
            with args.summary.open("a") as stream:
                stream.write(render_inputs(catalog) + "\n")
            print(f"Listed {len(catalog['candidates'])} base rootfs candidates")
        elif args.command == "fetch":
            source = json.loads(args.source.read_text())
            fetch_source(source, args.directory)
        elif args.command == "verify":
            source = json.loads((args.directory / "source.json").read_text())
            write_json(args.output, verify_input(args.directory, source))
        else:
            export_transform(args.directory, args.root, args.output, args.report)
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f"transform: {error}\n")


if __name__ == "__main__":
    main()

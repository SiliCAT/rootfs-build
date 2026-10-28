#!/usr/bin/env python3
"""Resolve registered targets and export build metadata; no recipe inheritance."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


REPOSITORY = Path(__file__).resolve().parents[1]


def resolve(target_id):
    targets = json.loads((REPOSITORY / "targets/targets.json").read_text())
    if target_id not in targets:
        raise ValueError(f"unknown target: {target_id}")
    target = dict(targets[target_id], id=target_id)
    fields = {"id", "architecture", "suite", "name", "recipe", "runner", "platform",
              "execution", "builder_image"}
    if set(target) != fields or any(not isinstance(value, str) or "\n" in value or "\r" in value
                                    for value in target.values()):
        raise ValueError("target must contain exactly the supported single-line string fields")
    for key in ("id", "architecture", "suite", "name"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", target[key]):
            raise ValueError(f"invalid target field: {key}")
    expected = f"recipes/{target['architecture']}/{target['suite']}/{target['name']}/recipe.sh"
    if target["recipe"] != expected:
        raise ValueError("recipe path does not match the target identity")
    recipe = (REPOSITORY / expected).resolve(strict=True)
    if not recipe.is_relative_to(REPOSITORY) or not recipe.is_file() or not os.access(recipe, os.X_OK):
        raise ValueError("recipe must be an executable inside the repository")
    # Only this native backend is implemented. Reject unsupported targets instead
    # of silently treating another architecture as an amd64 build.
    if (target["architecture"], target["platform"], target["runner"], target["execution"]) != (
        "amd64", "linux/amd64", "ubuntu-24.04", "native"
    ):
        raise ValueError("unsupported runner, architecture or execution backend")
    if not re.fullmatch(r"debian:trixie-slim@sha256:[0-9a-f]{64}", target["builder_image"]):
        raise ValueError("builder must be a digest-pinned Debian Trixie image")
    target["artifact_name"] = f"rootfs-{target['suite']}-{target['architecture']}-{target['name']}"
    return target


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_metadata(target_id, root, output):
    return export_rootfs_metadata(resolve(target_id), root, output)


def export_rootfs_metadata(target, root, output, extra=None):
    """Export a checked rootfs; callers supply its identity and optional provenance."""
    root = root.resolve(strict=True)
    output = output.resolve(strict=True)
    stem = target["artifact_name"]
    archive = output / f"{stem}.tar.zst"
    if not archive.is_file():
        raise ValueError("verified archive is missing")
    result = subprocess.run(
        ["dpkg-query", f"--admindir={root / 'var/lib/dpkg'}", "--show",
         "--showformat=${db:Status-Status}\t${binary:Package}\t${Version}\t${Architecture}\n"],
        check=True, text=True, capture_output=True, env=dict(os.environ, LC_ALL="C"),
    )
    packages = []
    for line in result.stdout.splitlines():
        status, package, version, architecture = line.split("\t")
        if status == "installed":
            packages.append((package, version, architecture))
    if not packages:
        raise ValueError("package manifest contains no installed packages")
    packages.sort()
    package_path = output / f"{stem}.packages.tsv"
    package_path.write_text("package\tversion\tarchitecture\n" +
                            "".join("\t".join(row) + "\n" for row in packages))
    kernels = sorted(p.name for p in (root / "lib/modules").iterdir() if p.is_dir())
    if not kernels:
        raise ValueError("no target kernels available for metadata")
    versions = {}
    for tool, arguments in {"mmdebstrap": ["--version"], "tar": ["--version"],
                            "zstd": ["--version"]}.items():
        version = subprocess.run([tool, *arguments], check=True, text=True, capture_output=True)
        versions[tool] = (version.stdout or version.stderr).splitlines()[0]
    metadata = {
        "target": target,
        "source_commit": os.environ.get("SOURCE_COMMIT", "unknown"),
        "source_repository": os.environ.get("SOURCE_REPOSITORY", "unknown"),
        "source_ref": os.environ.get("SOURCE_REF", "unknown"),
        "workflow_run_url": os.environ.get("WORKFLOW_RUN_URL"),
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "runner_kernel": os.uname().release,
        "target_kernels": kernels,
        "zfs_packages": {name: version for name, version, _ in packages if name.startswith("zfs")},
        "installed_package_count": len(packages),
        "builder_tools": versions,
        "archive": archive.name,
        "archive_sha256": sha256(archive),
        "boot_tested": False,
    }
    if extra:
        if set(extra) & set(metadata):
            raise ValueError("extra metadata cannot replace standard build fields")
        metadata.update(extra)
    metadata_path = output / f"{stem}.build.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    files = [archive, output / f"{archive.name}.sha256", package_path, metadata_path]
    sums = output / f"{stem}.SHA256SUMS"
    sums.write_text("".join(f"{sha256(path)}  {path.name}\n" for path in files))
    for path in [*files, sums]:
        path.chmod(0o644)
    print(f"Exported {len(packages)} package versions and {len(kernels)} target kernels")


def collect_diagnostics(state):
    state = state.resolve(strict=True)
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    patterns = (
        "work/rootfs-build.*/build.log",
        "work/rootfs-build.*/rootfs/var/lib/dkms/*/*/build/make.log",
        "work/rootfs-build.*/rootfs/var/lib/dkms/*/*/*/*/log/make.log",
        "work/rootfs-build.*/rootfs/var/log/apt/term.log",
        "work/rootfs-build.*/rootfs/var/log/dpkg.log",
    )
    for pattern in patterns:
        for source in state.glob(pattern):
            # Do not follow an absolute rootfs symlink into the runner filesystem.
            if source.is_symlink() or not source.resolve().is_relative_to(state) or not source.is_file():
                continue
            destination = logs / source.relative_to(state)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
    for directory, _, _ in os.walk(logs):
        Path(directory).chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    resolution = subparsers.add_parser("resolve")
    resolution.add_argument("target")
    resolution.add_argument("--github-output", type=Path)
    resolution.add_argument("--output", type=Path)
    export = subparsers.add_parser("export")
    export.add_argument("target")
    export.add_argument("rootfs", type=Path)
    export.add_argument("output", type=Path)
    diagnostics = subparsers.add_parser("diagnostics")
    diagnostics.add_argument("state", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            target = resolve(args.target)
            contents = json.dumps(target, indent=2) + "\n"
            if args.output:
                args.output.write_text(contents)
            else:
                print(contents, end="")
            if args.github_output:
                with args.github_output.open("a") as stream:
                    for key, value in target.items():
                        stream.write(f"{key}={value}\n")
        elif args.command == "export":
            export_metadata(args.target, args.rootfs, args.output)
        else:
            collect_diagnostics(args.state)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"target: {error}\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Verify packaged Raspberry Pi boot copies without probing builder hardware."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def regular_file(root, relative):
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing, empty or escaping file: {relative}")
    return path


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(root):
    root = root.resolve(strict=True)
    if root == Path("/") or not root.is_dir():
        raise ValueError("expected a rootfs directory other than /")
    versions = []
    for path in (root / "boot").glob("vmlinuz-*"):
        version = path.name.removeprefix("vmlinuz-")
        if version.endswith((".dpkg-bak", ".old-dkms")):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._+~-]+", version):
            raise ValueError("invalid target kernel filename")
        regular_file(root, f"boot/{path.name}")
        versions.append(version)
    if not versions:
        raise ValueError("no target kernels")
    # Match Debian's hook selection, then require its initrd to have that version.
    selected = subprocess.run(["sort", "-V"], input="\n".join(versions) + "\n",
                              text=True, capture_output=True, check=True,
                              env=dict(os.environ, LC_ALL="C")).stdout.splitlines()[-1]
    config = regular_file(root, "boot/firmware/config.txt").read_text()
    kernels = re.findall(r"^[ \t]*kernel[ \t]*=[ \t]*(\S+)[ \t]*$", config, re.M)
    initrds = re.findall(r"^[ \t]*initramfs[ \t]+(\S+)(?:[ \t]+\S+)?[ \t]*$", config, re.M)
    if kernels != [f"vmlinuz-{selected}"] or initrds != [f"initrd.img-{selected}"]:
        raise ValueError("firmware config does not reference a matching target kernel/initrd pair")
    for name in (kernels[0], initrds[0]):
        source = regular_file(root, f"boot/{name}")
        copy = regular_file(root, f"boot/firmware/{name}")
        if digest(source) != digest(copy):
            raise ValueError(f"stale firmware copy: {name}")
    dtb_directory = root / f"usr/lib/linux-image-{selected}/broadcom"
    dtbs = sorted(dtb_directory.glob("bcm*.dtb"))
    if not dtbs:
        raise ValueError(f"no packaged Broadcom DTBs for {selected}")
    for dtb in dtbs:
        source = regular_file(root, dtb.relative_to(root))
        copy = regular_file(root, f"boot/firmware/{dtb.name}")
        if digest(source) != digest(copy):
            raise ValueError(f"DTB differs from target kernel package: {dtb.name}")
    return {"kernel": selected, "kernel_file": kernels[0], "initrd_file": initrds[0],
            "dtbs": [path.name for path in dtbs]}


def installed(root):
    result = subprocess.run(
        ["dpkg-query", f"--admindir={root / 'var/lib/dpkg'}", "--show",
         "--showformat=${Package}\t${db:Status-Status}\n"],
        check=True, capture_output=True, text=True,
    )
    return "raspi-firmware\tinstalled" in result.stdout.splitlines()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--if-installed", action="store_true")
    args = parser.parse_args()
    try:
        if args.if_installed and not installed(args.root):
            print("[RASPI VERIFY] package absent; no Raspberry Pi copies to verify")
            return
        record = verify(args.root)
        print("[RASPI VERIFY] " + json.dumps(record, sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(95, f"[RASPI VERIFY][ERR] {error}\n")


if __name__ == "__main__":
    main()

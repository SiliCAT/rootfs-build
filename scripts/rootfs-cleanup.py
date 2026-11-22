#!/usr/bin/env python3
"""Common Trixie rootfs cleanup, builder-side only (C01-C09).

The caller owns an exclusive build directory. T1 follows package operations;
T2 follows mmdebstrap unmount/reaping. Verify is read-only. Unreviewed states
fail closed rather than being silently discarded. No target code is executed.
"""
import argparse
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile

HOSTS = b"127.0.0.1       localhost\n::1             localhost ip6-localhost ip6-loopback\nff02::1         ip6-allnodes\nff02::2         ip6-allrouters\n"
POLICY = b"#!/bin/sh\nexit 101\n"
FRAGMENT = "etc/dpkg/dpkg.cfg.d/99mmdebstrap"
HISTORY = (".bash_history", ".sh_history", ".python_history", ".lesshst", ".viminfo")


class Invalid(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def mountpoints():
    def decode(value):
        return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)
    points = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        require(len(fields) >= 10 and "-" in fields, "invalid mountinfo")
        points.append(Path(decode(fields[4])))
    require(points, "empty mountinfo")
    return points


class Cleanup:
    def __init__(self, root, stage, options=(), required=()):
        supplied = Path(root)
        require(supplied.is_absolute() and ".." not in supplied.parts, "absolute root required")
        self.root = supplied.resolve(strict=True)
        require(self.root == supplied and self.root != Path("/") and self.root.is_dir(),
                "root must be a real canonical directory other than /")
        require(os.geteuid() == 0, "root privileges required")
        self.stage = stage
        self.check = stage == "verify"
        if not self.check:
            # The recipe issues this record only after checking its empty
            # build directory. It must live outside the tree being cleaned.
            lease_name = os.environ.get("ROOTFS_CLEANUP_LEASE", "")
            require(bool(lease_name), "missing exclusive build authorization")
            lease = Path(lease_name)
            require(lease.is_absolute() and lease.resolve(strict=True) == lease
                    and self.root != lease and self.root not in lease.parents,
                    "invalid build authorization path")
            st = lease.lstat()
            require(stat.S_ISREG(st.st_mode) and st.st_uid == 0 and st.st_nlink == 1
                    and stat.S_IMODE(st.st_mode) == 0o600,
                    "invalid build authorization metadata")
            require(lease.read_bytes() == os.fsencode(self.root), "build authorization root mismatch")
        self.options = list(options)
        self.required = list(required)
        self.mounts = mountpoints()
        self.guard(self.root, stage != "t1")
        for name in ("etc", "usr", "var"):
            require(self.kind(self.path(name)) == "dir", "missing rootfs directory: " + name)

    def log(self, rule, name, status):
        print("[CLEAN][%s][%s][%s] %s" % (self.stage.upper(), rule, status, name), flush=True)

    def guard(self, path, recursive=False):
        for m in self.mounts:
            if m == self.root or m == path or (m in path.parents and self.root in m.parents):
                raise Invalid("mounted cleanup path: " + str(path.relative_to(self.root)))
            if recursive and path in m.parents:
                raise Invalid("nested mount: " + str(path.relative_to(self.root)))

    @staticmethod
    def kind(path):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(mode):
            return "link"
        if stat.S_ISDIR(mode):
            return "dir"
        if stat.S_ISREG(mode):
            return "file"
        return "special"

    def path(self, name):
        rel = Path(name)
        require(not rel.is_absolute() and ".." not in rel.parts and rel.parts, "unsafe relative path")
        here = self.root
        for part in rel.parts[:-1]:
            here /= part
            require(self.kind(here) in (None, "dir"), "unsafe ancestor: " + str(here.relative_to(self.root)))
        p = self.root / rel
        self.guard(p)
        return p

    def regular(self, p):
        require(self.kind(p) == "file", "expected regular file: " + str(p.relative_to(self.root)))
        st = p.stat()
        require(st.st_nlink == 1, "hard-linked file: " + str(p.relative_to(self.root)))
        return st

    def data(self, p):
        self.regular(p)
        return p.read_bytes()

    def text(self, p):
        return self.data(p).decode("utf-8")

    def entries(self, p):
        require(self.kind(p) == "dir", "expected real directory: " + str(p.relative_to(self.root)))
        self.guard(p, True)
        return sorted(p.iterdir())

    def absent(self, rule, name):
        require(self.kind(self.path(name)) is None, rule + " unreviewed object: " + name)
        self.log(rule, name, "SKIP")

    def remove(self, rule, name, directory=False):
        p = self.path(name)
        k = self.kind(p)
        if k is None:
            self.log(rule, name, "SKIP")
            return
        require(not self.check, rule + " residual object: " + name)
        require(k in ("file", "link") or (directory and k == "dir"), "unexpected type: " + name)
        self.guard(p, True)
        self.log(rule, name, "ACT")
        if k == "dir":
            shutil.rmtree(p)
        else:
            p.unlink()
        require(self.kind(p) is None, "remove failed: " + name)
        self.log(rule, name, "OK")

    def empty_dir(self, rule, name):
        p = self.path(name)
        if self.kind(p) is None:
            self.log(rule, name, "SKIP")
            return
        items = self.entries(p)
        require(not self.check or not items, rule + " nonempty directory: " + name)
        for item in items:
            self.remove(rule, str(item.relative_to(self.root)), True)
        require(not self.entries(p), "directory remains nonempty: " + name)
        self.log(rule, name, "OK")

    def reset(self, rule, name, content=b"", mode=None, required=False):
        p = self.path(name)
        k = self.kind(p)
        if k is None and not required:
            self.log(rule, name, "SKIP")
            return
        require(k in (None, "file"), "unexpected file type: " + name)
        old = self.regular(p) if k else None
        if mode is None:
            require(old is not None, "missing metadata: " + name)
            owner, group, perm = old.st_uid, old.st_gid, stat.S_IMODE(old.st_mode)
        else:
            owner, group, perm = 0, 0, mode
        same = old is not None and self.data(p) == content and (
            old.st_uid, old.st_gid, stat.S_IMODE(old.st_mode)) == (owner, group, perm)
        require(not self.check or same, rule + " unexpected final state: " + name)
        if not same:
            self.log(rule, name, "ACT")
            if mode is None:
                # Existing single-link file: retain metadata including xattrs.
                with p.open("wb") as out:
                    out.write(content)
            else:
                fd, tmp = tempfile.mkstemp(prefix=".rootfs-clean-", dir=p.parent)
                try:
                    with os.fdopen(fd, "wb") as out:
                        out.write(content)
                        os.fchown(out.fileno(), owner, group)
                        os.fchmod(out.fileno(), perm)
                    os.replace(tmp, p)
                finally:
                    if os.path.lexists(tmp):
                        os.unlink(tmp)
        result = self.regular(p)
        require(self.data(p) == content and (
            result.st_uid, result.st_gid, stat.S_IMODE(result.st_mode)) == (owner, group, perm),
            "final state mismatch: " + name)
        self.log(rule, name, "OK")

    def active(self, p):
        return [line.strip() for line in self.text(p).splitlines()
                if line.strip() and not line.lstrip().startswith(("#", ";"))]

    def inspect_file(self, rule, name, allowed):
        p = self.path(name)
        if self.kind(p) is None:
            self.log(rule, name, "SKIP")
            return
        require(allowed(self.active(p)), rule + " unreviewed active configuration: " + name)
        self.log(rule, name, "OK")

    def inspect_tree(self, rule, name, allowed=None):
        p = self.path(name)
        if self.kind(p) is None:
            self.log(rule, name, "SKIP")
            return
        for item in self.entries(p):
            rel = str(item.relative_to(self.root))
            if self.kind(item) == "dir":
                self.inspect_tree(rule, rel, allowed)
            elif allowed is not None:
                self.inspect_file(rule, rel, allowed)
            else:
                raise Invalid(rule + " unreviewed state: " + rel)
        self.log(rule, name, "OK")

    def package_state(self):
        installed = {}
        for paragraph in self.text(self.path("var/lib/dpkg/status")).split("\n\n"):
            fields = dict(line.split(": ", 1) for line in paragraph.splitlines()
                          if line and not line[0].isspace() and ": " in line)
            if "Package" not in fields:
                continue
            status = fields.get("Status", "").split()
            require(len(status) == 3 and status[1] == "ok" and status[2] in ("installed", "not-installed"),
                    "C08 unexpected package state: " + fields["Package"])
            if status[2] == "installed":
                installed[fields["Package"]] = fields.get("Version", "")
        require("dpkg" in installed, "C08 dpkg database missing")
        for name in self.required:
            require(name in installed, "C08 missing required package: " + name)
        return installed

    def homes(self):
        homes = {"root"}
        for line in self.text(self.path("etc/passwd")).splitlines():
            fields = line.split(":")
            require(len(fields) == 7, "invalid passwd record")
            if fields[5].startswith("/home/"):
                p = Path(fields[5][1:])
                require(len(p.parts) == 2 and ".." not in p.parts, "unreviewed user home")
                homes.add(str(p))
        # Check existing home directories even when no passwd entry exists.
        p = self.path("home")
        if self.kind(p) is not None:
            for item in self.entries(p):
                require(self.kind(item) == "dir", "unreviewed home entry")
                homes.add(str(item.relative_to(self.root)))
        return sorted(homes)

    def logs(self, name):
        p = self.path(name)
        if self.kind(p) is None:
            self.log("C09.03", name, "SKIP")
            return
        for item in self.entries(p):
            rel = str(item.relative_to(self.root))
            if self.kind(item) == "dir":
                self.logs(rel)
            elif rel == "var/log/apt/eipp.log.xz":
                self.remove("C09.03", rel)
            else:
                data = self.data(item)
                require(not data or item.name in ("wtmp", "btmp", "lastlog", "faillog") or
                        (not re.search(r"\.(gz|xz|zst|bz2|zip|journal)(\.\d+)?$", item.name)
                         and b"\x00" not in data), "unknown binary/compressed log: " + rel)
                self.reset("C09.03", rel)

    def run(self):
        packages = self.package_state()
        # C01
        for name in ("etc/hostname", "etc/machine-id"):
            self.reset("C01", name, mode=0o644, required=True)
        self.reset("C01.02", "etc/hosts", HOSTS, 0o644, True)
        for name in ("var/lib/dbus/machine-id", "var/lib/systemd/random-seed", "etc/machine-info",
                     "etc/hostname-", "etc/hosts-", "etc/machine-id-", "etc/machine-info-"):
            self.remove("C01", name)
        self.absent("C01.08", "etc/hostid")

        # C02: only after network operations.
        self.reset("C02.01", "etc/resolv.conf", mode=0o644, required=True)
        for name in ("var/lib/dhcp", "var/lib/dhcpcd", "var/lib/systemd/rfkill"):
            self.empty_dir("C02", name)
        self.inspect_file("C02.04", "etc/udev/rules.d/70-persistent-net.rules", lambda lines: not lines)
        for name in ("var/lib/NetworkManager", "var/lib/systemd/network"):
            self.inspect_tree("C02.05", name)
        network = lambda lines: all(re.fullmatch(
            r"(auto lo|iface lo inet loopback|source(-directory)? /etc/network/interfaces\.d(?:/\*)?)", s)
            for s in lines)
        self.inspect_file("C02.06", "etc/network/interfaces", network)
        for name in ("etc/network/interfaces.d", "etc/systemd/network", "etc/netplan",
                     "etc/NetworkManager/system-connections"):
            self.inspect_tree("C02.06", name, network)

        # C03: preserve unknown credentials for diagnosis, never print contents.
        ssh = self.path("etc/ssh")
        if self.kind(ssh) is not None:
            for item in self.entries(ssh):
                if item.name.startswith("ssh_host_"):
                    self.remove("C03.01", str(item.relative_to(self.root)))
        else:
            self.log("C03.01", "etc/ssh", "SKIP")
        roots = [line.split(":")[1] for line in self.text(self.path("etc/shadow")).splitlines()
                 if line.startswith("root:")]
        require(len(roots) == 1 and re.fullmatch(r"[!*]+", roots[0]), "C03.02 root password not hash-free and locked")
        self.log("C03.02", "etc/shadow (root)", "OK")
        homes = self.homes()
        for home in homes:
            for name in ("known_hosts", "known_hosts.old"):
                self.remove("C03.03", home + "/.ssh/" + name)
            for name in (".ssh", ".config/incus", ".incus"):
                self.inspect_tree("C03.03/04", home + "/" + name)
        self.inspect_file("C03.05", "etc/apt/auth.conf", lambda lines: not lines)
        self.inspect_tree("C03.05", "etc/apt/auth.conf.d", lambda lines: not lines)

        # C04.01: keep the package-generated initiator identity unchanged.
        # Clones must not use a shared IQN as independent initiators.
        # Instance provisioning owns any identity assignment before iSCSI use.
        name = "etc/iscsi/initiatorname.iscsi"
        if self.kind(self.path(name)) is None:
            self.log("C04.01", name, "SKIP")
        else:
            self.log("C04.01", name + " (native identity retained; review at deployment)", "KEEP")

        def iscsi_config(lines):
            for line in lines:
                key, sep, value = line.partition("=")
                key, value = key.strip().lower(), value.strip()
                if not sep:
                    return False
                if re.search(r"(username|password|initiatorname|targetname|portal|hwaddress|net_ifacename|ipaddress|discovery_address)", key):
                    return False
                if key.endswith(".auth.authmethod") and value.lower() != "none":
                    return False
            return True
        self.inspect_file("C04.02", "etc/iscsi/iscsid.conf", iscsi_config)
        for parent in ("var/lib/iscsi", "etc/iscsi"):
            for child in ("nodes", "send_targets", "static", "isns", "slp"):
                self.empty_dir("C04.03/04", parent + "/" + child)
            self.inspect_tree("C04.05", parent + "/ifaces", lambda lines: all(
                re.fullmatch(r"iface\.(transport_name|iface_name)\s*=\s*(tcp|iser|default)", s) for s in lines))
        self.inspect_file("C04.05", "etc/iscsi/iscsi.initramfs", lambda lines: not lines)

        # C05: no live device probing. Pending semantics are inspection-only.
        for name in ("etc/zfs/zpool.cache", "etc/lvm/devices/system.devices"):
            self.absent("C05", name)
        for name in ("etc/lvm/backup", "etc/lvm/archive"):
            self.empty_dir("C05.03", name)
        if self.stage != "t1":
            self.empty_dir("C05.04", "run/lvm")
        self.inspect_file("C05.05", "etc/mdadm/mdadm.conf", lambda lines: all(
            re.fullmatch(r"(HOMEHOST\s+<system>|MAILADDR\s+root|DEVICE\s+partitions\s+containers)", s) for s in lines))
        for name in ("etc/fstab", "etc/crypttab"):
            self.inspect_file("C05.06/07", name, lambda lines: not lines)

        # C06: NFS directory structure/ownership remains.
        self.remove("C06.01", "var/lib/incus", True)
        self.inspect_tree("C06.02", "var/lib/ceph")
        self.inspect_tree("C06.02", "etc/ceph", lambda lines: not lines)
        for name in ("var/lib/nfs/sm", "var/lib/nfs/sm.bak"):
            self.empty_dir("C06.03", name)
        for name in ("var/lib/nfs/state", "var/lib/nfs/etab", "var/lib/nfs/rmtab"):
            p = self.path(name)
            if self.kind(p) is not None:
                require(self.data(p) == b"", "C06.04/05 unreviewed NFS state: " + name)
                self.log("C06.04/05", name, "OK")
            else:
                self.log("C06.04/05", name, "SKIP")
        self.inspect_tree("C06.06", "var/lib/nfs/nfsdcltrack")
        self.remove("C06.07", "var/log/journal", True)
        self.remove("C06.08", "var/lib/systemd/timesync/clock")

        # C07
        for name in ("etc/subuid", "etc/subgid", "etc/subuid-", "etc/subgid-"):
            p = self.path(name)
            if self.kind(p) is None:
                self.log("C07.01", name, "SKIP")
                continue
            content = self.data(p)
            filtered = b"".join(line for line in content.splitlines(keepends=True)
                                if line.split(b":", 1)[0] != b"root")
            self.reset("C07.01", name, filtered)

        # C08: package actions already completed by T1.
        p = self.path("var/cache/apt/archives")
        if self.kind(p) is not None:
            for item in self.entries(p):
                if item.name == "lock":
                    require(self.data(item) == b"", "unexpected APT archive lock content")
                elif item.name == "partial":
                    self.empty_dir("C08.04", "var/cache/apt/archives/partial")
                else:
                    raise Invalid("C08 unexpected APT archive entry")
        self.log("C08.01-05", "package status / cache", "OK")
        self.empty_dir("C08.06", "var/lib/apt/lists")
        p = self.path(FRAGMENT)
        if self.kind(p) is not None:
            require(self.options and self.active(p) == self.options, "C08.07 foreign dpkg fragment")
            self.remove("C08.07", FRAGMENT)
        else:
            self.log("C08.07", FRAGMENT, "SKIP")
        self.remove("C08.08", "etc/build-meta")
        if self.stage != "t1":
            p = self.path("usr/sbin/policy-rc.d")
            if self.kind(p) is not None:
                require(self.data(p) == POLICY, "C08.09 foreign policy-rc.d")
            self.remove("C08.09", "usr/sbin/policy-rc.d")
            self.absent("C08.07", "etc/apt/apt.conf.d/00mmdebstrap")

        # C09
        for name in ("tmp", "var/tmp", "var/crash"):
            self.empty_dir("C09.01/06", name)
        if self.stage != "t1":
            self.inspect_tree("C09.02", "run")
        self.logs("var/log")
        for home in homes:
            for name in HISTORY:
                self.remove("C09.04", home + "/" + name)
            self.inspect_tree("C09.07", home + "/.cache")
        self.remove("C09.05", "var/cache/ldconfig/aux-cache")

        # First adopter has no startup images. Do not silently pass an image
        # before the common boot artifact readers have been implemented.
        boot = self.path("boot")
        if self.kind(boot) is not None:
            for item in self.entries(boot):
                require(not item.name.startswith(("initrd", "initramfs")) and item.name != "firmware",
                        "C04.06 startup artifact requires image-specific inspection")
        self.log("C04.06", "boot images (none)", "SKIP")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("t1", "t2", "verify"))
    parser.add_argument("root")
    args = parser.parse_args()
    try:
        Cleanup(args.root, args.stage,
                os.environ.get("ROOTFS_BUILD_DPKG_OPTIONS", "").splitlines(),
                os.environ.get("ROOTFS_REQUIRED_PACKAGES", "").split()).run()
    except (Invalid, OSError, UnicodeError, ValueError) as exc:
        print("[CLEAN][ERR] " + str(exc), file=sys.stderr)
        return 95
    print("[CLEAN][%s][OK] C01-C09 finished" % args.stage.upper())
    return 0


if __name__ == "__main__":
    sys.exit(main())

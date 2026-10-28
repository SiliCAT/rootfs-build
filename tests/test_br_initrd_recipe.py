"""Execute the bridge payload with fixture cmdline/sysfs and harmless network tools."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

RECIPE = Path(__file__).resolve().parents[1] / "transforms/trixie/br-initrd/recipe.sh"
SOURCE = RECIPE.read_text()
PAYLOAD = SOURCE.split("#===PAYLOAD:10-br-exp:BEGIN===\n", 1)[1].split(
    "#===PAYLOAD:10-br-exp:END===", 1)[0]


class BridgeRecipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="br-initrd-tests-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.net = self.base / "net"
        self.net.mkdir()
        self.log = self.base / "kmsg"
        self.commands = self.base / "commands"
        self.sleeps = self.base / "sleeps"
        self.script = self.base / "payload"
        # A regular log fixture needs append semantics to emulate /dev/kmsg.
        self.script.write_text(PAYLOAD.replace("> /dev/kmsg", ">> /dev/kmsg")
                               .replace("/proc/cmdline", str(self.base / "cmdline"))
                               .replace("/sys/class/net", str(self.net))
                               .replace("/dev/kmsg", str(self.log)))
        self.write_tool("ip", f"printf '%s\\n' \"$*\" >> '{self.commands}'\n")
        self.write_tool("modprobe", "exit 1\n")
        self.write_tool("sleep", f"printf 'wait\\n' >> '{self.sleeps}'\n")
        self.interface("eth0", "aa:bb:cc:dd:ee:ff")

    def write_tool(self, name, body):
        path = self.tools / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def interface(self, name, mac):
        path = self.net / name
        path.mkdir()
        (path / "address").write_text(mac + "\n")
        (path / "type").write_text("1\n")
        (path / "carrier").write_text("1\n")

    def run_payload(self, options):
        (self.base / "cmdline").write_text(options + "\n")
        result = subprocess.run(["sh", str(self.script)], capture_output=True, text=True, timeout=5,
                                env=dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ["PATH"]))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.commands.read_text() if self.commands.exists() else ""

    def test_recipe_and_all_embedded_shells_have_valid_syntax(self):
        subprocess.run(["bash", "-n", str(RECIPE)], check=True, capture_output=True)
        subprocess.run(["sh", "-n"], input=PAYLOAD, text=True, check=True, capture_output=True)
        for tag, body in re.findall(r"<<'([A-Z_]+)'\n(.*?)\n\1(?:\n|$)", SOURCE, re.S):
            if tag in {"HELP", "REPORT", "BR_INITRD_EMBEDDED_PAYLOAD"}:
                continue
            with self.subTest(tag=tag):
                subprocess.run(["bash", "-n"], input=body, text=True, check=True, capture_output=True)

    def test_disabled_bridge_does_not_run_network_commands(self):
        self.assertEqual(self.run_payload("ip=:::::br0:dhcp br_if=eth0"), "")

    def test_manual_selection_creates_bridge_and_enslaves_requested_interface(self):
        commands = self.run_payload("br_exp=1 ip=:::::br0:dhcp br_if=eth0")
        self.assertIn("link add name br0 type bridge", commands)
        self.assertIn("link set eth0 master br0", commands)

    def test_mac_selection_overrides_manual_interface_and_normalizes_uppercase(self):
        commands = self.run_payload(
            "br_exp=1 ip=:::::br0:dhcp br_if=wrong br_if_mac=AA:BB:CC:DD:EE:FF")
        self.assertIn("link set eth0 master br0", commands)
        self.assertNotIn("link set wrong", commands)

    def test_explicit_empty_mac_never_falls_back_to_valid_manual_interface(self):
        commands = self.run_payload("br_exp=1 ip=:::::br0:dhcp br_if=eth0 br_if_mac=")
        self.assertNotIn("link add name br0", commands)
        self.assertIn("Invalid br_if_mac", self.log.read_text())

    def test_duplicate_mac_matches_fail_without_fallback(self):
        self.interface("eth1", "aa:bb:cc:dd:ee:ff")
        commands = self.run_payload(
            "br_exp=1 ip=:::::br0:dhcp br_if=eth0 br_if_mac=aa:bb:cc:dd:ee:ff")
        self.assertNotIn("link add name br0", commands)
        self.assertIn("matched 2 interfaces", self.log.read_text())

    def test_missing_mac_uses_ten_waits_then_exits_without_manual_fallback(self):
        commands = self.run_payload(
            "br_exp=1 ip=:::::br0:dhcp br_if=eth0 br_if_mac=00:11:22:33:44:55")
        self.assertNotIn("link add name br0", commands)
        self.assertEqual(self.sleeps.read_text().splitlines(), ["wait"] * 10)
        self.assertIn("11 scan(s)", self.log.read_text())

    def test_auto_selection_uses_successful_dhcp_probe(self):
        self.write_tool("udhcpc", "exit 0\n")
        commands = self.run_payload("br_exp=1 ip=:::::br0:dhcp")
        self.assertIn("link set eth0 master br0", commands)

    def test_build_context_restores_original_regular_files_and_dangling_symlinks(self):
        function = SOURCE.split("restore_context() {\n", 1)[1].split("\n}\nfor path", 1)[0]
        root = self.base / "root"
        state = self.base / "state"
        state.mkdir()
        (root / "etc").mkdir(parents=True)
        (root / "usr/sbin").mkdir(parents=True)
        (state / "resolv.conf").symlink_to("../run/resolvconf/resolv.conf")
        policy = state / "policy-rc.d"
        policy.write_text("#!/bin/sh\nexit 77\n")
        policy.chmod(0o751)
        (root / "etc/resolv.conf").write_text("temporary build DNS")
        (root / "usr/sbin/policy-rc.d").write_text("temporary service policy")
        subprocess.run(["bash", "-Eeuo", "pipefail", "-c",
                        "restore_context() {\n" + function + "\n}\nrestore_context\nrestore_context\n"],
                       env=dict(os.environ, DST_ROOT=str(root), STATE_DIR=str(state), CONTEXT_READY="1"),
                       check=True, capture_output=True)
        self.assertEqual(os.readlink(root / "etc/resolv.conf"), "../run/resolvconf/resolv.conf")
        self.assertEqual((root / "usr/sbin/policy-rc.d").read_bytes(), policy.read_bytes())
        self.assertEqual((root / "usr/sbin/policy-rc.d").stat().st_mode & 0o777, 0o751)

    def test_build_context_removes_temporary_files_when_originals_were_absent(self):
        function = SOURCE.split("restore_context() {\n", 1)[1].split("\n}\nfor path", 1)[0]
        root, state = self.base / "root", self.base / "state"
        state.mkdir()
        for name in ("etc/resolv.conf", "usr/sbin/policy-rc.d"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("temporary")
        subprocess.run(["bash", "-Eeuo", "pipefail", "-c",
                        "restore_context() {\n" + function + "\n}\nrestore_context\n"],
                       env=dict(os.environ, DST_ROOT=str(root), STATE_DIR=str(state), CONTEXT_READY="1"),
                       check=True, capture_output=True)
        self.assertFalse((root / "etc/resolv.conf").exists())
        self.assertFalse((root / "usr/sbin/policy-rc.d").exists())


if __name__ == "__main__":
    unittest.main()

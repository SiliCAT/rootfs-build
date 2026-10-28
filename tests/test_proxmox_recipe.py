"""Check the standalone recipe interface and execute its owned-config cleanup.

The capture fixture never installs packages or runs hooks. Actual system behavior
is also checked by the recipe's verify.sh during the real build.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
RECIPE = REPOSITORY / "recipes/amd64/trixie/proxmox/recipe.sh"


class ProxmoxRecipeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="proxmox-recipe-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "target with space\nand newline"
        self.root.mkdir()
        tools = self.base / "tools"
        tools.mkdir()
        if os.geteuid() != 0:
            self.executable(tools / "id", "#!/bin/sh\nprintf '0\\n'\n")
        self.executable(tools / "mmdebstrap", '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ['PROXMOX_TEST_CAPTURE']).write_text(json.dumps({
    'args': sys.argv[1:],
    'apt': os.environ['PROXMOX_BUILD_APT_CONFIG'],
    'dpkg': os.environ['PROXMOX_BUILD_DPKG_CONFIG'],
}))
raise SystemExit(37)
''')
        self.environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"],
                                PROXMOX_TEST_CAPTURE=str(self.base / "capture.json"))
        result = subprocess.run([str(RECIPE), str(self.root)], env=self.environment,
                                cwd="/", text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.capture = json.loads((self.base / "capture.json").read_text())
        self.args = self.capture["args"]
        self.cleanup = self.hooks("customize")[-1]

    @staticmethod
    def executable(path, text):
        path.write_text(text)
        path.chmod(0o755)

    def hooks(self, name):
        return [arg.split("=", 1)[1] for arg in self.args if arg.startswith(f"--{name}-hook=")]

    def config_fixture(self):
        apt = self.root / "etc/apt/apt.conf.d/90-proxmox-build"
        dpkg = self.root / "etc/dpkg/dpkg.cfg.d/99mmdebstrap"
        for path, contents in [(apt, self.capture["apt"]), (dpkg, self.capture["dpkg"])]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents + "\n")
        return apt, dpkg

    def run_cleanup(self):
        environment = dict(self.environment, PROXMOX_BUILD_APT_CONFIG=self.capture["apt"],
                           PROXMOX_BUILD_DPKG_CONFIG=self.capture["dpkg"])
        return subprocess.run(["sh", "-c", self.cleanup, "hook", str(self.root)],
                              env=environment, text=True, capture_output=True, timeout=5)

    def test_package_business_contract(self):
        include = next(arg.split("=", 1)[1] for arg in self.args if arg.startswith("--include="))
        packages = re.split(r"[,\s]+", include)
        self.assertEqual(len(packages), 44)
        self.assertEqual(len(set(packages)), 44)
        self.assertTrue({"incus-base", "apparmor", "apparmor-utils", "qemu-guest-agent", "open-vm-tools"} <= set(packages))
        self.assertTrue({"systemd-container", "nfs-kernel-server", "autofs", "mdadm",
                         "linux-headers-amd64", "dpkg-dev", "firmware-intel-graphics"}.isdisjoint(packages))
        self.assertFalse(any(package.startswith("zfs") for package in packages))

    def test_native_directory_build_and_sources(self):
        self.assertIn("--mode=root", self.args)
        self.assertIn("--format=directory", self.args)
        self.assertFalse(any(arg.startswith("--architectures=") for arg in self.args))
        self.assertEqual(self.args[-6], str(self.root))
        self.assertEqual(self.args[-5:], [
            "deb https://deb.debian.org/debian trixie main contrib non-free non-free-firmware",
            "deb https://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware",
            "deb https://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware",
            "deb https://deb.debian.org/debian trixie-backports main contrib non-free non-free-firmware",
            "deb-src https://deb.debian.org/debian trixie-backports main contrib non-free non-free-firmware",
        ])

    def test_early_extraction_uses_native_dpkg_filter(self):
        options = [arg.split("=", 1)[1] for arg in self.args if arg.startswith("--dpkgopt=")]
        self.assertEqual(options, [self.capture["dpkg"]])
        self.assertEqual(options[0].splitlines(), [
            "path-exclude=/usr/share/doc/*", "path-exclude=/usr/share/man/*",
            "path-exclude=/usr/share/locale/*",
        ])
        self.assertFalse(any(arg.startswith("--aptopt=") for arg in self.args))

    def test_cleanup_removes_only_owned_build_configs(self):
        apt, dpkg = self.config_fixture()
        other_apt = apt.with_name("50-distribution")
        other_dpkg = dpkg.with_name("10-distribution")
        other_apt.write_text('APT::Install-Recommends "true";\n')
        other_dpkg.write_text("# distribution policy\n")
        result = self.run_cleanup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(apt.exists())
        self.assertFalse(dpkg.exists())
        self.assertEqual(other_apt.read_text(), 'APT::Install-Recommends "true";\n')
        self.assertEqual(other_dpkg.read_text(), "# distribution policy\n")

    def test_cleanup_rejects_replaced_configuration_without_deleting_it(self):
        apt, dpkg = self.config_fixture()
        changed = self.capture["dpkg"] + "\npath-include=/unrelated/*\n"
        dpkg.write_text(changed)
        result = self.run_cleanup()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(apt.exists())
        self.assertEqual(dpkg.read_text(), changed)

    def test_hook_and_chroot_script_syntax(self):
        for hook in self.hooks("setup") + self.hooks("customize"):
            with self.subTest(hook=hook[:80]):
                subprocess.run(["sh", "-n"], input=hook, text=True, check=True, capture_output=True)
                marker = "sh -euxs <<'EOF'\n"
                if marker in hook:
                    body = hook.split(marker, 1)[1].split("\nEOF", 1)[0]
                    subprocess.run(["sh", "-n"], input=body, text=True, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()

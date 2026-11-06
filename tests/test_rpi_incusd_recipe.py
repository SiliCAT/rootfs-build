"""Capture the standalone recipe contract; never run package hooks in these tests."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

REPOSITORY = Path(__file__).resolve().parents[1]
RECIPE = REPOSITORY / "recipes/arm64/trixie/rpi-incusd/recipe.sh"


class RpiIncusdRecipeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rpi-incusd-tests-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "root with space\nand newline"
        self.root.mkdir()
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.tool('id', '#!/bin/sh\nprintf "0\\n"\n')
        self.tool('dpkg', '#!/bin/sh\nprintf "%s\\n" "${TEST_ARCH:-arm64}"\n')
        self.tool('mmdebstrap', '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))
raise SystemExit(37)
''')
        self.environment = dict(os.environ, PATH=str(self.tools)+os.pathsep+os.environ['PATH'],
                                CAPTURE=str(self.base/'args.json'))

    def tool(self, name, contents):
        path = self.tools/name
        path.write_text(contents)
        path.chmod(0o755)

    def capture(self):
        result = subprocess.run([str(RECIPE), str(self.root)], env=self.environment,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 37, result.stdout+result.stderr)
        return json.loads((self.base/'args.json').read_text())

    def test_package_set_preserves_original_arm_recipe(self):
        args = self.capture()
        packages = next(arg.split('=', 1)[1] for arg in args if arg.startswith('--include=')).split()
        expected = '''init systemd systemd-sysv dbus netbase iproute2 curl sudo tzdata locales
        dialog apt-utils ca-certificates autofs nfs-common ifupdown systemd-timesyncd zstd xz-utils
        lz4 bzip2 gzip iputils-ping procps ethtool xfsprogs btrfs-progs bcache-tools mdadm lvm2
        nbd-client open-iscsi systemd-zram-generator busybox-static rsync openssh-server incus-base
        apparmor apparmor-utils linux-image-arm64 linux-headers-arm64 firmware-misc-nonfree dpkg-dev
        zfsutils-linux/trixie-backports zfs-dkms/trixie-backports'''.split()
        self.assertCountEqual(packages, expected)
        self.assertEqual(len(packages), len(set(packages)))
        hooks = '\n'.join(arg for arg in args if arg.startswith('--customize-hook='))
        self.assertIn('install -y --no-install-recommends raspi-firmware', hooks)

    def test_native_directory_and_original_source_suites(self):
        args = self.capture()
        for flag in ('--mode=root', '--format=directory', '--architectures=arm64'):
            self.assertIn(flag, args)
        self.assertEqual(args[-4], str(self.root))
        self.assertEqual(args[-3:], [
            'deb https://deb.debian.org/debian trixie main contrib non-free non-free-firmware',
            'deb https://deb.debian.org/debian trixie-backports main contrib non-free-firmware',
            'deb-src https://deb.debian.org/debian trixie-backports main contrib non-free-firmware'])

    def test_non_native_builder_is_rejected_before_bootstrap(self):
        result = subprocess.run([str(RECIPE), str(self.root)], env=dict(self.environment, TEST_ARCH='amd64'),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 95)
        self.assertFalse((self.base/'args.json').exists())

    def test_job_wrapper_rejects_wrong_native_builder_for_each_architecture(self):
        for target, actual, expected in (
                ('arm64-trixie-rpi-incusd', 'amd64', 'arm64'),
                ('amd64-trixie-memini', 'arm64', 'amd64')):
            with self.subTest(target=target):
                state = self.base/target
                state.mkdir()
                result = subprocess.run(
                    [str(REPOSITORY/'scripts/run-target.sh'), target, str(state)],
                    env=dict(self.environment, TEST_ARCH=actual), capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
                self.assertIn(f'Native {expected} builder required', result.stderr)
                self.assertFalse((self.base/'args.json').exists())

    def test_hooks_have_valid_shell_syntax_and_no_instance_policies(self):
        args = self.capture()
        for hook in (arg.split('=', 1)[1] for arg in args if re.match('--(?:setup|customize)-hook=', arg)):
            subprocess.run(['sh', '-n'], input=hook, text=True, check=True, capture_output=True)
            marker = "sh -euxs <<'EOF'\n"
            if marker in hook:
                body = hook.split(marker, 1)[1].split('\nEOF', 1)[0]
                subprocess.run(['sh', '-n'], input=body, text=True, check=True, capture_output=True)
            self.assertNotIn('zfs_arc_max', hook)
            self.assertNotIn('/etc/subuid', hook)
            self.assertNotIn('/etc/subgid', hook)
        setup = '\n'.join(arg for arg in args if arg.startswith('--setup-hook='))
        self.assertIn('MODULES=most', setup)

    def test_unified_menu_contains_exactly_registered_targets(self):
        registry = json.loads((REPOSITORY/'targets/targets.json').read_text())
        menu = (REPOSITORY/'.github/workflows/rootfs.yml').read_text().split('        options:\n', 1)[1]
        options = re.findall(r'^          - (\S+)$', menu, re.M)
        self.assertCountEqual(options, registry)

"""Exercise the real archive shell with a tiny recipe, without building Debian.

These tests require real root and mount-namespace privileges. Archive creation,
verification, restoration, cancellation, mounts and locking use the actual tools.
"""

import ctypes.util
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
BUILD = REPOSITORY / "scripts/build-rootfs.sh"
VERIFY = REPOSITORY / "scripts/verify-rootfs.sh"


class ArchiveShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest("requires root and private mount-namespace privileges")
        result = subprocess.run(["unshare", "--mount", "--propagation", "private", "--", "true"],
                                capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise unittest.SkipTest("cannot create private mount namespaces: " + result.stderr.strip())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="archive-shell-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.tools = self.base / "tools"
        self.tools.mkdir()
        self.work = self.base / "work with space\nand newline"
        self.work.mkdir()
        self.recipe = self.base / "recipe with space.sh"
        self.output = self.base / "output" / "rootfs.tar.zst"
        self.environment = dict(os.environ, PATH=str(self.tools) + os.pathsep + os.environ["PATH"])
        self.write_recipe()

    @staticmethod
    def executable(path, contents):
        path.write_text(contents)
        path.chmod(0o755)

    def write_recipe(self, extra=""):
        self.executable(self.recipe, "#!/usr/bin/env python3\n" + textwrap.dedent('''
            import ctypes
            import ctypes.util
            import json
            import os
            from pathlib import Path
            import subprocess
            import sys
            import time

            assert len(sys.argv) == 2
            root = Path(sys.argv[1])
            assert root.is_absolute() and root.is_dir() and not root.is_symlink()
            assert not list(root.iterdir())
            assert root.stat().st_mode & 0o777 == 0o755
            for name in ['etc', 'usr/bin', 'var', 'dev']:
                (root / name).mkdir(parents=True)
            (root / 'etc/os-release').write_text('ID=fixture\\n')
            data = root / 'etc/data'
            data.write_bytes(b'payload\\x00with data\\n')
            os.setxattr(data, 'user.rootfs-test', b'extended metadata')
            os.utime(data, ns=(1700000000123456789, 1700000000123456789))
            (root / '.hidden').write_text('hidden content')
            tool = root / 'usr/bin/tool'
            tool.write_text('#!/bin/sh\\nexit 0\\n')
            tool.chmod(0o751)
            os.link(tool, root / 'usr/bin/hardlink')
            (root / 'etc/relative-link').symlink_to('../usr/bin/tool')
            (root / 'etc/absolute-link').symlink_to('/usr/bin/tool')
            os.mkfifo(root / 'dev/fixture-fifo', 0o640)
            with (root / 'var/sparse').open('wb') as sparse:
                sparse.seek(8 * 1024 * 1024)
                sparse.write(b'end')
            library = ctypes.util.find_library('acl')
            if library:
                acl_file = root / 'etc/acl-file'
                acl_file.write_text('ACL fixture')
                lib = ctypes.CDLL(library, use_errno=True)
                lib.acl_from_text.argtypes = [ctypes.c_char_p]
                lib.acl_from_text.restype = ctypes.c_void_p
                lib.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
                lib.acl_free.argtypes = [ctypes.c_void_p]
                acl = lib.acl_from_text(('user::rw-\\nuser:' + str(os.getuid()) + ':r--\\ngroup::r--\\nmask::r--\\nother::---\\n').encode())
                assert acl
                rc = lib.acl_set_file(os.fsencode(acl_file), 0x8000, acl)
                if rc == 0:
                    (root / 'acl-supported').touch()
                elif ctypes.get_errno() != 95:
                    raise OSError(ctypes.get_errno(), 'cannot create ACL fixture')
                lib.acl_free(acl)
            print('fixture recipe completed', flush=True)
        ''') + textwrap.dedent(extra))

    def arguments(self, *extra):
        return [str(BUILD), "--recipe", str(self.recipe), "--output", str(self.output),
                "--work-dir", str(self.work), "--threads", "1", *extra]

    def run_build(self, *extra):
        return subprocess.run(self.arguments(*extra), env=self.environment, cwd="/",
                              capture_output=True, text=True, timeout=20)

    def assert_failed_without_archive(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertFalse(self.output.exists())
        self.assertFalse(Path(str(self.output) + ".sha256").exists())
        self.assertEqual(len(list(self.work.glob("rootfs-build.*"))), 1)

    def restore(self):
        restored = self.base / "restored"
        restored.mkdir()
        subprocess.run(["tar", "--zstd", "--extract", "--file=" + str(self.output),
                        "--directory=" + str(restored), "--same-permissions", "--numeric-owner",
                        "--acls", "--xattrs", "--xattrs-include=*", "--selinux"], check=True,
                       capture_output=True)
        return restored

    def test_archive_round_trip_preserves_content_and_metadata(self):
        result = self.run_build("--keep-work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.restore()
        source = next(self.work.glob("rootfs-build.*")) / "rootfs"
        self.assertEqual((restored / "etc/data").read_bytes(), (source / "etc/data").read_bytes())
        self.assertEqual((restored / ".hidden").read_text(), "hidden content")
        self.assertEqual((restored / "etc/data").stat().st_mtime_ns, 1700000000123456789)
        self.assertEqual(os.getxattr(restored / "etc/data", "user.rootfs-test"), b"extended metadata")
        self.assertEqual(stat.S_IMODE((restored / "usr/bin/tool").stat().st_mode), 0o751)
        self.assertEqual((restored / "usr/bin/tool").stat().st_ino,
                         (restored / "usr/bin/hardlink").stat().st_ino)
        self.assertEqual(os.readlink(restored / "etc/relative-link"), "../usr/bin/tool")
        self.assertEqual(os.readlink(restored / "etc/absolute-link"), "/usr/bin/tool")
        self.assertTrue(stat.S_ISFIFO((restored / "dev/fixture-fifo").stat().st_mode))
        self.assertEqual((restored / "var/sparse").stat().st_size, 8 * 1024 * 1024 + 3)
        self.assertLess((restored / "var/sparse").stat().st_blocks * 512, 1024 * 1024)
        self.assertFalse((restored / "rootfs").exists())
        self.assertIn("fixture recipe completed", (source.parent / "build.log").read_text())
        check = subprocess.run(["sha256sum", "--check", self.output.name + ".sha256"],
                               cwd=self.output.parent, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_acl_round_trip(self):
        result = self.run_build("--keep-work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source = next(self.work.glob("rootfs-build.*")) / "rootfs"
        if not (source / "acl-supported").exists():
            self.skipTest("libacl or filesystem POSIX ACL support is unavailable")
        restored = self.restore()
        self.assertEqual(os.getxattr(restored / "etc/acl-file", "system.posix_acl_access"),
                         os.getxattr(source / "etc/acl-file", "system.posix_acl_access"))

    def test_success_removes_work_and_refuses_overwrite(self):
        first = self.run_build()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(list(self.work.iterdir()), [])
        digest = hashlib.sha256(self.output.read_bytes()).digest()
        second = self.run_build()
        self.assertEqual(second.returncode, 5)
        self.assertEqual(hashlib.sha256(self.output.read_bytes()).digest(), digest)

    def test_recipe_failure_preserves_status_and_work(self):
        self.write_recipe("raise SystemExit(37)\n")
        self.assert_failed_without_archive(self.run_build(), 37)

    def test_recipe_isolates_mounts_without_changing_user_namespace(self):
        self.write_recipe('''
            (root / 'etc/namespaces.json').write_text(json.dumps({
                'mount': os.readlink('/proc/self/ns/mnt'),
                'user': os.readlink('/proc/self/ns/user'),
                'uid': os.getuid(),
            }))
        ''')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        metadata = json.loads((self.restore() / "etc/namespaces.json").read_text())
        self.assertNotEqual(metadata["mount"], os.readlink("/proc/self/ns/mnt"))
        self.assertEqual(metadata["user"], os.readlink("/proc/self/ns/user"))
        self.assertEqual(metadata["uid"], 0)

    def efi_mount_fixture(self):
        # Cleanup is a safeguard for running this regression against old code.
        # Only these known synthetic mounts, below this test's private tree, are touched.
        self.addCleanup(self.cleanup_fixture_mounts)
        return textwrap.dedent('''
            (root / 'sys').mkdir()
            subprocess.run(['mount', '-t', 'tmpfs', 'rootfs-test-sys', str(root / 'sys')], check=True)
            efivars = root / 'sys/firmware/efi/efivars'
            efivars.mkdir(parents=True)
            subprocess.run(['mount', '-t', 'tmpfs', 'rootfs-test-efivars', str(efivars)], check=True)
            (efivars / 'runtime-only').write_text('must not enter the archive')
            busy = subprocess.run(['umount', str(root / 'sys')], text=True, capture_output=True)
            assert busy.returncode != 0
            print('simulated mmdebstrap cleanup warning: ' + busy.stderr, flush=True)
        ''')

    def cleanup_fixture_mounts(self):
        for root in self.work.glob("rootfs-build.*/rootfs"):
            for relative in ("sys/firmware/efi/efivars", "sys"):
                subprocess.run(["umount", str(root / relative)], capture_output=True, timeout=5)

    def assert_no_fixture_mounts(self):
        source = next(self.work.glob("rootfs-build.*")) / "rootfs"
        result = subprocess.run([str(VERIFY), str(source)], env=self.environment,
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(list((source / "sys").iterdir()), [])

    def test_nested_efi_mounts_disappear_before_archive(self):
        self.write_recipe(self.efi_mount_fixture())
        result = self.run_build("--keep-work")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("simulated mmdebstrap cleanup warning", result.stdout)
        self.assert_no_fixture_mounts()
        self.assertEqual(list((self.restore() / "sys").iterdir()), [])

    def test_failed_recipe_releases_nested_mounts_and_preserves_status(self):
        self.write_recipe(self.efi_mount_fixture() + "\nraise SystemExit(37)\n")
        self.assert_failed_without_archive(self.run_build(), 37)
        self.assert_no_fixture_mounts()

    def test_namespace_creation_failure_never_runs_recipe(self):
        self.executable(self.tools / "unshare", "#!/bin/sh\nexit 42\n")
        self.assert_failed_without_archive(self.run_build(), 42)
        source = next(self.work.glob("rootfs-build.*")) / "rootfs"
        self.assertEqual(list(source.iterdir()), [])

    def test_optional_recipe_verifier_failure(self):
        self.executable(self.recipe.parent / "verify.sh", "#!/bin/sh\nexit 47\n")
        self.assert_failed_without_archive(self.run_build(), 47)

    def test_compression_failure_preserves_status(self):
        self.executable(self.tools / "zstd", "#!/bin/sh\nexit 23\n")
        self.assert_failed_without_archive(self.run_build(), 23)

    def test_archive_compare_detects_source_change(self):
        real_tar = shutil.which("tar")
        self.executable(self.tools / "tar", "#!/usr/bin/env python3\n" + textwrap.dedent(f'''
            import os, sys
            from pathlib import Path
            if '--compare' in sys.argv:
                root = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--directory='))
                (Path(root) / 'etc/data').write_text('changed after archiving')
            os.execv({real_tar!r}, [{real_tar!r}, *sys.argv[1:]])
        '''))
        self.assert_failed_without_archive(self.run_build(), 1)

    def test_corrupt_compressed_archive_cannot_be_published(self):
        real_zstd = shutil.which("zstd")
        self.executable(self.tools / "zstd", "#!/usr/bin/env python3\n" + textwrap.dedent(f'''
            import subprocess, sys
            from pathlib import Path
            result = subprocess.run([{real_zstd!r}, *sys.argv[1:]])
            if result.returncode == 0 and '-o' in sys.argv:
                path = Path(sys.argv[sys.argv.index('-o') + 1])
                data = bytearray(path.read_bytes())
                data[-1] ^= 0xFF
                path.write_bytes(data)
            raise SystemExit(result.returncode)
        '''))
        self.assert_failed_without_archive(self.run_build(), 1)

    def test_publication_failure_retains_source_and_removes_own_checksum(self):
        real_ln = shutil.which("ln")
        self.executable(self.tools / "ln", "#!/usr/bin/env python3\n" + textwrap.dedent(f'''
            import os, sys
            if sys.argv[-1].endswith('.tar.zst'):
                raise SystemExit(38)
            os.execv({real_ln!r}, [{real_ln!r}, *sys.argv[1:]])
        '''))
        self.assert_failed_without_archive(self.run_build(), 38)
        source = next(self.work.glob("rootfs-build.*")) / "rootfs"
        self.assertTrue((source / "etc/data").exists())
        self.assertTrue((source.parent / "build.log").exists())

    def mount_shim(self, content):
        real_awk = shutil.which("awk")
        self.executable(self.tools / "awk", "#!/usr/bin/env python3\n" + textwrap.dedent(f'''
            import os, sys
            from pathlib import Path
            args = sys.argv[1:]
            if '/proc/self/mountinfo' in args:
                path = os.environ['MOUNT_QUERY_PATH'] + '/usr/src'
                path = path.replace('\\\\', '\\\\134').replace(' ', '\\\\040').replace('\\t', '\\\\011').replace('\\n', '\\\\012')
                fixture = Path({str(self.base / 'mountinfo')!r})
                fixture.write_text({content!r}.replace('TARGET', path))
                args[args.index('/proc/self/mountinfo')] = str(fixture)
            os.execv({real_awk!r}, [{real_awk!r}, *args])
        '''))

    def test_residual_bind_mount_is_rejected_including_escaped_paths(self):
        self.mount_shim("1 0 0:1 / / rw - ext4 /dev/root rw\n"
                        "2 1 0:1 /host/usr/src TARGET rw - ext4 /dev/root rw\n")
        result = self.run_build()
        self.assert_failed_without_archive(result, 95)
        self.assertIn("residual mount", result.stdout)

    def test_malformed_mount_table_fails_closed(self):
        self.mount_shim("unreadable mount data\n")
        self.assert_failed_without_archive(self.run_build(), 95)

    def test_existing_output_symlink_is_not_followed(self):
        self.output.parent.mkdir()
        victim = self.base / "victim"
        victim.write_text("preserve")
        self.output.symlink_to(victim)
        result = self.run_build()
        self.assertEqual(result.returncode, 5)
        self.assertEqual(victim.read_text(), "preserve")
        self.assertEqual(list(self.work.iterdir()), [])

    def test_concurrent_output_lock(self):
        self.output.parent.mkdir()
        with Path(str(self.output) + ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_build()
        self.assertEqual(result.returncode, 75)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_term_stops_recipe_and_child_and_retains_work(self):
        self.write_recipe(self.efi_mount_fixture() + textwrap.dedent('''
            child = subprocess.Popen(['sleep', '30'])
            (root / 'child.pid').write_text(str(child.pid))
            print('waiting for cancellation', flush=True)
            time.sleep(30)
        '''))
        process = subprocess.Popen(self.arguments(), env=self.environment, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            markers = list(self.work.glob("rootfs-build.*/rootfs/child.pid"))
            if markers:
                break
            time.sleep(0.02)
        else:
            self.fail("recipe did not start")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 143, stdout + stderr)
        self.assertFalse(self.output.exists())
        child_pid = int(markers[0].read_text())
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if proc_stat.exists():
            self.assertEqual(proc_stat.read_text().split(") ", 1)[1].split()[0], "Z")
        self.assertTrue(markers[0].exists())
        self.assert_no_fixture_mounts()

    def test_live_background_writer_prevents_publication(self):
        self.write_recipe("subprocess.Popen(['sleep', '30'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
        self.assert_failed_without_archive(self.run_build(), 95)

    def test_writer_in_a_different_process_group_prevents_publication(self):
        self.write_recipe("subprocess.Popen(['sleep', '30'], preexec_fn=os.setpgrp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n")
        self.assert_failed_without_archive(self.run_build(), 95)

    def test_filename_with_newline_and_spaces(self):
        self.output = self.base / "output" / "root fs\nfixture.tar.zst"
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        check = subprocess.run(["sha256sum", "--check", self.output.name + ".sha256"],
                               cwd=self.output.parent, capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_verify_rejects_symlink_with_trailing_slashes(self):
        target = self.base / "target"
        target.mkdir()
        linked = self.base / "linked"
        linked.symlink_to(target, target_is_directory=True)
        result = subprocess.run([str(VERIFY), str(linked) + "///"], capture_output=True)
        self.assertEqual(result.returncode, 5)

    @unittest.skipUnless(os.geteuid() == 0, "requires real root to create UID 1000033 files")
    def test_high_numeric_owner_round_trip(self):
        self.write_recipe("os.chown(root / 'etc/data', 1000033, 1000033)\n")
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        restored = self.restore()
        info = (restored / "etc/data").stat()
        self.assertEqual((info.st_uid, info.st_gid), (1000033, 1000033))


if __name__ == "__main__":
    unittest.main()

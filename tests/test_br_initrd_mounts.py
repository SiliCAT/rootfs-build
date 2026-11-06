"""Exercise the recipe's actual mount inspection/release functions and publisher."""

import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import unittest

REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE = (REPOSITORY / 'transforms/trixie/br-initrd/recipe.sh').read_text()
BUILD = REPOSITORY / 'scripts/build-rootfs.sh'


def shell_function(name):
    start = re.search(r'^' + re.escape(name) + r'\(\) \{', SOURCE, re.M).start()
    line_end = SOURCE.index('\n', start)
    if SOURCE[start:line_end].endswith('}'):
        return SOURCE[start:line_end] + '\n'
    end = SOURCE.index('\n}\n', line_end) + 3
    return SOURCE[start:end]


FUNCTIONS = '\n'.join(shell_function(name) for name in (
    'emit', 'warn', 'run', 'inspect_view', 'release_views'))


def escaped(path):
    return str(path).replace('\\', r'\134').replace(' ', r'\040').replace('\t', r'\011').replace('\n', r'\012')


class MountSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='br-mountinfo-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.view = self.base / 'root with space\nand literal\\040' / 'sys'
        self.table = self.base / 'mountinfo'

    def table_lines(self, root_options='', child_parent=10):
        view = escaped(self.view)
        return ('1 0 0:1 / / rw - rootfs rootfs rw\n'
                f'10 1 0:2 / {view} rw {root_options}- tmpfs root-view rw\n'
                f'11 {child_parent} 0:3 / {view}/firmware/efi/efivars rw - tmpfs child-view rw\n')

    def run_snapshot(self, table, action):
        self.table.write_text(table)
        script = FUNCTIONS + '''
awk() {
    local args=("$@")
    args[${#args[@]}-1]=$MOUNT_TABLE
    command awk "${args[@]}"
}
umount() { printf 'UNEXPECTED_UNMOUNT\\n'; return 77; }
VIEW_PATHS=("$VIEW/dev" "$VIEW/proc" "$VIEW")
VIEW_IDS=('' '' "${RECORDED_ID:-10}")
VIEW_ATTEMPTED=(0 0 1)
STAGE=TEST
''' + action
        return subprocess.run(['bash', '-Eeuo', 'pipefail', '-c', script],
                              env=dict(os.environ, VIEW=str(self.view), MOUNT_TABLE=str(self.table)),
                              capture_output=True, text=True, timeout=5)

    def test_snapshot_decodes_paths_and_reports_parent_type_and_source(self):
        result = self.run_snapshot(self.table_lines(), '''
inspect_view "$VIEW"
[[ $VIEW_ID == 10 && $VIEW_CHILDREN == 1 && $VIEW_PRIVATE == 1 ]]
printf '%s\\n' "$VIEW_MOUNTS"
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('id=11 parent_id=10', result.stdout)
        self.assertIn('target=' + escaped(self.view) + '/firmware/efi/efivars', result.stdout)
        self.assertIn('type=tmpfs source=child-view', result.stdout)

    def test_replaced_root_is_reported_but_not_unmounted(self):
        result = self.run_snapshot(self.table_lines().replace('10 1 ', '20 1 ').replace('11 10 ', '11 20 '),
                                   'if release_views; then exit 90; fi\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('view ownership cannot be confirmed', result.stderr)
        self.assertIn('id=11 parent_id=20', result.stderr)
        self.assertNotIn('UNEXPECTED_UNMOUNT', result.stdout)

    def test_shared_subtree_is_reported_but_not_unmounted(self):
        tables = (self.table_lines(root_options='shared:7 '),
                  self.table_lines().replace('rw - tmpfs child-view', 'rw shared:7 - tmpfs child-view'))
        for table in tables:
            with self.subTest(table=table):
                result = self.run_snapshot(table, 'if release_views; then exit 90; fi\n')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('view subtree is not private', result.stderr)
                self.assertIn('shared:7', result.stderr)
                self.assertNotIn('UNEXPECTED_UNMOUNT', result.stdout)

    def test_malformed_or_unrelated_mounts_are_not_unmounted(self):
        for table in (self.table_lines() + 'malformed mount row\n',
                      self.table_lines(child_parent=1),
                      self.table_lines() + f'12 1 0:4 / {escaped(self.view)} rw - tmpfs overmount rw\n'):
            with self.subTest(table=table):
                result = self.run_snapshot(table, 'if release_views; then exit 90; fi\n')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('cannot inspect view', result.stderr)
                self.assertNotIn('UNEXPECTED_UNMOUNT', result.stdout)


class MountReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0:
            raise unittest.SkipTest('requires root and mount namespaces')
        probe = subprocess.run(['unshare', '--mount', '--propagation', 'private', '--', 'true'],
                               capture_output=True, text=True, timeout=5)
        if probe.returncode:
            raise unittest.SkipTest('mount namespace unavailable: ' + probe.stderr.strip())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='br-release-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.work = self.base / 'work with space\nand newline'
        self.work.mkdir()
        self.recipe = self.base / 'fixture.sh'
        self.output = self.base / 'output/rootfs.tar.xz'

    def make_recipe(self, body):
        self.recipe.write_text('#!/usr/bin/env bash\nset -Eeuo pipefail\n' + FUNCTIONS + '''
DST_ROOT=$1
STAGE=TEST
mkdir -p "$DST_ROOT"/{etc,usr,var,dev,proc,sys,unowned}
printf 'persistent payload\\n' > "$DST_ROOT/etc/payload"
VIEW_PATHS=("$DST_ROOT/dev" "$DST_ROOT/proc" "$DST_ROOT/sys")
VIEW_IDS=('' '' '')
VIEW_ATTEMPTED=(0 0 0)
for index in 0 1 2; do
    mount -t tmpfs "br-test-view-$index" "${VIEW_PATHS[index]}"
    mount --make-private "${VIEW_PATHS[index]}"
    inspect_view "${VIEW_PATHS[index]}"
    VIEW_IDS[index]=$VIEW_ID
    VIEW_ATTEMPTED[index]=1
done
child=$DST_ROOT/sys/firmware/efi/efivars
mkdir -p "$child"
mount -t tmpfs br-test-child "$child"
# Include stacked mounts and a grandchild; recursive cleanup must handle both.
mount -t tmpfs br-test-overmount "$child"
mkdir "$child/nested"
mount -t tmpfs br-test-grandchild "$child/nested"
printf 'runtime only\\n' > "$child/nested/runtime-only"
mount -t tmpfs br-test-unowned "$DST_ROOT/unowned"
printf 'leave unrelated mounts alone\\n' > "$DST_ROOT/unowned/sentinel"
''' + body)
        self.recipe.chmod(0o755)

    def run_build(self):
        result = subprocess.run([str(BUILD), '--recipe', str(self.recipe), '--output', str(self.output),
                                 '--work-dir', str(self.work), '--compression-level', '0',
                                 '--threads', '1', '--keep-work'], capture_output=True, text=True, timeout=30)
        # In either outcome the outer namespace must leave no caller-visible mounts.
        for root in self.work.glob('rootfs-build.*/rootfs'):
            for name in ('dev', 'proc', 'sys', 'unowned'):
                check = subprocess.run(['mountpoint', '-q', str(root/name)], capture_output=True)
                self.assertNotEqual(check.returncode, 0, f'leaked mount: {root/name}')
        return result

    def test_recursive_release_removes_nested_mounts_and_allows_archive(self):
        self.make_recipe('''
release_views || exit 95
[[ ${VIEW_ATTEMPTED[*]} == '0 0 0' ]]
release_views # Idempotent: already absent views are not touched again.
mountpoint -q "$DST_ROOT/unowned"
[[ $(cat "$DST_ROOT/unowned/sentinel") == 'leave unrelated mounts alone' ]]
umount -- "$DST_ROOT/unowned"
''')
        result = self.run_build()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('source=br-test-grandchild', result.stdout)
        self.assertRegex(result.stdout, r'id=\d+ parent_id=\d+ target=.* type=tmpfs')
        self.assertIn('umount --recursive --', result.stdout)
        with tarfile.open(self.output, 'r:xz') as archive:
            self.assertEqual(archive.extractfile('./etc/payload').read(), b'persistent payload\n')
            self.assertFalse(any('runtime-only' in name or 'sentinel' in name for name in archive.getnames()))

    def test_real_busy_mount_failure_prevents_archive_publication(self):
        self.make_recipe('''
# A child process holding its cwd inside the subtree makes real umount fail.
( cd "$child/nested"; exec sleep 30 ) &
holder=$!
# The child announces readiness through procfs rather than a timed sleep.
for ((attempt=0; attempt<10000; attempt++)); do
    [[ $(readlink "/proc/$holder/cwd") == "$child/nested" ]] && break
done
[[ $(readlink "/proc/$holder/cwd") == "$child/nested" ]]
if release_views; then
    kill "$holder"; wait "$holder" || true
    exit 90
fi
kill "$holder"; wait "$holder" || true
exit 95
''')
        result = self.run_build()
        self.assertEqual(result.returncode, 95, result.stdout + result.stderr)
        self.assertIn('cannot unmount owned view', result.stdout)
        self.assertIn('source=br-test-grandchild', result.stdout)
        self.assertFalse(self.output.exists())

    def test_replaced_view_is_not_unmounted_and_prevents_archive(self):
        self.make_recipe('''
original_id=${VIEW_IDS[2]}
# Keep the original mount alive at another path, preventing mount-ID reuse.
mkdir "$DST_ROOT/moved-sys"
mount --move "$DST_ROOT/sys" "$DST_ROOT/moved-sys"
mount -t tmpfs br-test-replacement "$DST_ROOT/sys"
mount --make-private "$DST_ROOT/sys"
inspect_view "$DST_ROOT/sys"
replacement_id=$VIEW_ID
[[ $replacement_id != "$original_id" ]]
if release_views; then exit 90; fi
inspect_view "$DST_ROOT/sys"
[[ $VIEW_ID == "$replacement_id" ]]
exit 95
''')
        result = self.run_build()
        self.assertEqual(result.returncode, 95, result.stdout + result.stderr)
        self.assertIn('view ownership cannot be confirmed', result.stdout)
        self.assertIn('source=br-test-replacement', result.stdout)
        self.assertFalse(self.output.exists())

#!/bin/sh
set -eu

[ "$#" -eq 1 ] || { printf 'Usage: verify.sh ABSOLUTE_TARGET_ROOT\n' >&2; exit 2; }

chroot "$1" /bin/sh -es <<'EOF_CHROOT'
fail() { printf '[SCHROOT-MAINTENANCE VERIFY][ERR] %s\n' "$1" >&2; exit 95; }

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[ "$(dpkg --print-architecture)" = amd64 ] || fail 'unexpected architecture'
audit=$(dpkg --audit)
[ -z "$audit" ] || fail "$audit"

for tool in apt-get tar gzip parted partprobe fdisk sfdisk kpartx blkid \
    debugfs mdir xfs_db btrfs dump.f2fs fsck.erofs unsquashfs qemu-img \
    smartctl nvme mdadm cryptsetup nbd-client iscsiadm showmount rpcinfo incus; do
    command -v "$tool" >/dev/null || fail "missing tool: $tool"
done
[ "$(dpkg-query -W -f='${db:Status-Status}' incus-client)" = installed ] ||
    fail 'incus-client is missing'
for package in incus incus-base xorriso; do
    if status=$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null); then
        [ "$status" != installed ] || fail "unexpected package: $package"
    fi
done

visudo -cf /etc/sudoers
[ "$(stat -c '%u:%g:%a' /etc/sudoers.d/00-sudo-nopasswd)" = 0:0:440 ] ||
    fail 'unexpected sudoers ownership or permissions'
grep -Fxq '%sudo ALL=(ALL:ALL) NOPASSWD:ALL' /etc/sudoers.d/00-sudo-nopasswd ||
    fail 'passwordless sudo rule for the sudo group is missing'
root_status=$(passwd -S root)
read -r _ password_state _ <<EOF_STATUS
$root_status
EOF_STATUS
[ "$password_state" = L ] || fail 'root password must be locked'

policy_status=0
/usr/sbin/policy-rc.d || policy_status=$?
[ "$policy_status" -eq 101 ] || fail 'policy-rc.d must exit 101'

# Check the target regional settings independently of the builder environment.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for package in tzdata locales; do
    status=$(dpkg-query -W -f='${db:Status-Status}' "$package") ||
        fail "missing package: $package"
    [ "$status" = installed ] || fail "missing package: $package"
done

grep -Fxq 'en_US.UTF-8 UTF-8' /etc/locale.gen ||
    fail 'en_US.UTF-8 is not enabled in locale.gen'
available_locales=$(LC_ALL=C locale -a)
printf '%s\n' "$available_locales" | grep -Fxiq en_US.utf8 ||
    fail 'en_US.UTF-8 was not generated'
for target_locale in C.UTF-8 en_US.UTF-8; do
    charmap=$(LC_ALL="$target_locale" locale charmap 2>&1) ||
        fail "unavailable UTF-8 locale: $target_locale"
    [ "$charmap" = UTF-8 ] || fail "unexpected charmap: $target_locale"
done
[ -r /usr/share/locale/locale.alias ] && [ -r /etc/locale.alias ] ||
    fail 'locale.alias is missing or unreadable'

unset LANG LANGUAGE LC_ALL
[ -f /etc/locale.conf ] || fail 'missing /etc/locale.conf'
# shellcheck disable=SC1091
. /etc/locale.conf
[ "${LANG:-}" = en_US.UTF-8 ] || fail 'unexpected default LANG'
[ -z "${LC_ALL+x}" ] || fail 'LC_ALL must not be set globally'

[ -L /etc/localtime ] || fail '/etc/localtime must be a symbolic link'
zone=$(readlink -e /etc/localtime) || fail 'cannot resolve /etc/localtime'
[ "$zone" = /usr/share/zoneinfo/America/New_York ] ||
    fail 'unexpected timezone link target'
unset TZ
# Fixed winter and summer timestamps avoid a transition-time race.
for epoch in 1767225600 1782864000; do
    actual=$(LC_ALL=C date -d "@$epoch" '+%Z %z')
    expected=$(LC_ALL=C TZ=America/New_York date -d "@$epoch" '+%Z %z')
    [ "$actual" = "$expected" ] || fail 'unexpected effective timezone'
done

# Verify package-owned zoneinfo files, not deliberately filtered documentation
# or message translations. Require the dpkg verification command to succeed.
timezone_audit=$(dpkg -V tzdata) || fail 'cannot verify tzdata'
zoneinfo_audit=$(printf '%s\n' "$timezone_audit" |
    awk '/\/usr\/share\/zoneinfo\//')
[ -z "$zoneinfo_audit" ] || fail "tzdata zoneinfo integrity check failed: $zoneinfo_audit"
printf '[VERIFY] Regional settings checks passed\n'
[ -f /etc/machine-id ] && [ ! -s /etc/machine-id ] || fail 'machine-id must be empty'
[ ! -e /etc/dpkg/dpkg.cfg.d/99mmdebstrap ] || fail 'build-only dpkg exclusions remain'
[ -f /usr/share/doc/coreutils/copyright ] || fail 'copyright file is missing'
[ ! -e /usr/share/man/man1/cat.1.gz ] || fail 'man pages were not filtered'
[ ! -e /usr/share/man/man1/curl.1.gz ] || fail 'man pages from additional packages were not filtered'

for directory in /var/lib/apt/lists /tmp /var/tmp; do
    entry=$(find "$directory" -mindepth 1 -print -quit)
    [ -z "$entry" ] || fail "directory is not empty: $directory"
done
printf '[SCHROOT-MAINTENANCE VERIFY] package, policy and cleanup checks passed\n'
EOF_CHROOT

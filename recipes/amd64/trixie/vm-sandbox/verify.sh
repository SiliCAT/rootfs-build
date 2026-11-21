#!/usr/bin/env bash
set -euo pipefail

[[ $# == 1 && $1 == /* && $1 != / && ! -L $1 && -d $1 ]] || {
    printf 'Usage: verify.sh ABSOLUTE_TARGET_ROOT\n' >&2
    exit 2
}
root=$1
fail() { printf '[VM-SANDBOX VERIFY][ERR] %s\n' "$1" >&2; exit 95; }

for path in etc/apt/apt.conf.d/90-vm-sandbox-build etc/apt/apt.conf.d/99mmdebstrap \
    etc/dpkg/dpkg.cfg.d/99mmdebstrap etc/build-meta usr/sbin/policy-rc.d \
    var/lib/dbus/machine-id var/lib/systemd/random-seed var/log/journal; do
    [[ ! -e $root/$path && ! -L $root/$path ]] || fail "unexpected build or runtime state: $path"
done
[[ -f $root/etc/machine-id && ! -s $root/etc/machine-id ]] || fail 'machine-id must be empty'

# Query in one checked operation: an absent optional package is not a query error.
# These are dpkg format fields, not shell variables.
# shellcheck disable=SC2016
packages=$(chroot "$root" dpkg-query --show '--showformat=${binary:Package} ${db:Status-Status}\n')
for package in ceph-common qemu-guest-agent open-vm-tools linux-image-amd64; do
    awk -v wanted="$package" '
        { split($1, name, ":") }
        name[1]==wanted && $2=="installed" { found=1 }
        END { exit !found }
    ' <<< "$packages" || fail "required package is not installed: $package"
done
for package in systemd-container incus-base apparmor apparmor-utils; do
    if awk -v wanted="$package" '
        { split($1, name, ":") }
        name[1]==wanted && $2=="installed" { found=1 }
        END { exit !found }
    ' <<< "$packages"; then
        fail "excluded package must not be installed: $package"
    fi
done

# Version probes use local tools only; no cluster connection or device mapping.
for tool in ceph rados rbd; do
    chroot "$root" "$tool" --version
done
[[ -x $root/usr/sbin/mount.ceph ]] || fail 'mount.ceph is missing'
chroot "$root" getent passwd ceph >/dev/null || fail 'Ceph package user is missing'
for directory in var/lib/ceph var/log/ceph; do
    [[ -d $root/$directory ]] || fail "Ceph package directory is missing: $directory"
done
for unit in ceph.target rbdmap.service; do
    # Inspect package enablement without systemctl, /proc or a live systemd.
    override=$root/etc/systemd/system/$unit
    [[ ! -e $override && ! -L $override ]] || fail "unexpected Ceph unit override: $unit"
    wants=/etc/systemd/system/multi-user.target.wants/$unit
    [[ -L $root$wants ]] || fail "Debian service enablement is missing: $unit"
    vendor=$(chroot "$root" readlink -e "/usr/lib/systemd/system/$unit")
    enabled=$(chroot "$root" readlink -e "$wants")
    [[ $enabled == "$vendor" ]] || fail "Debian service enablement was changed: $unit"
done

chroot "$root" /bin/sh -es <<'EOF_REGIONAL'
fail() { printf '[VERIFY REGIONAL][ERR] %s\n' "$1" >&2; exit 95; }
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
EOF_REGIONAL
[[ $(chroot "$root" id -u silicat) == 1000033 ]] || fail 'unexpected silicat UID'
groups=$(chroot "$root" id -nG silicat)
[[ " $groups " == *' sudo '* ]] || fail 'silicat must belong to sudo'
root_status=$(chroot "$root" passwd -S root)
read -r _ password_state _ <<< "$root_status"
[[ $password_state == L ]] || fail 'root must be locked'
chroot "$root" visudo -cf /etc/sudoers
audit=$(chroot "$root" dpkg --audit)
[[ -z $audit ]] || fail "incomplete package state: $audit"

for unit in rsyslog.service qemu-guest-agent.service open-vm-tools.service; do
    mask=$root/etc/systemd/system/$unit
    [[ -L $mask && $(readlink -- "$mask") == /dev/null ]] || fail "missing service mask: $unit"
done

# These are real package payloads: coreutils is extracted in the essential stage;
# curl is installed later. Checking both catches a late-only exclusion policy.
for path in usr/share/doc/coreutils/copyright usr/share/man/man1/cat.1.gz \
    usr/share/man/man1/curl.1.gz usr/share/locale/fr/LC_MESSAGES/coreutils.mo; do
    [[ ! -e $root/$path && ! -L $root/$path ]] || fail "package payload was not filtered: $path"
done

for directory in tmp var/tmp var/lib/apt/lists var/lib/dhcp; do
    if [[ -d $root/$directory ]]; then
        entry=$(find "$root/$directory" -mindepth 1 -maxdepth 1 -print -quit)
        [[ -z $entry ]] || fail "directory is not empty: $directory"
    fi
done
shopt -s nullglob
host_keys=("$root"/etc/ssh/ssh_host_*)
(( ${#host_keys[@]} == 0 )) || fail 'SSH host keys remain'

kernel_count=0
for modules in "$root"/lib/modules/*; do
    [[ -d $modules ]] || continue
    version=${modules##*/}
    [[ -s $root/boot/vmlinuz-$version && -s $root/boot/initrd.img-$version ]] ||
        fail "missing kernel or initramfs: $version"
    kernel_count=$((kernel_count + 1))
done
(( kernel_count > 0 )) || fail 'no target kernels found'
printf '[VM-SANDBOX VERIFY] Ceph client, package, locale, policy, cleanup and kernel checks passed\n'

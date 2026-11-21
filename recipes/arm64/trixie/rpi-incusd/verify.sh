#!/usr/bin/env bash
set -euo pipefail

[[ $# == 1 && $1 == /* && $1 != / && ! -L $1 && -d $1 ]] || {
    printf 'Usage: verify.sh ABSOLUTE_TARGET_ROOT\n' >&2
    exit 2
}
root=$1
repository=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
fail() { printf '[RPI-INCUSD VERIFY][ERR] %s\n' "$1" >&2; exit 95; }
[[ $(chroot "$root" dpkg --print-architecture) == arm64 ]] || fail 'expected arm64 rootfs'
for path in etc/build-meta usr/sbin/policy-rc.d var/lib/dbus/machine-id \
    var/lib/systemd/random-seed var/lib/incus var/log/journal \
    etc/modprobe.d/90-zfs-arc-rpi4-4g.conf; do
    [[ ! -e $root/$path && ! -L $root/$path ]] || fail "unexpected build or instance state: $path"
done
[[ -f $root/etc/machine-id && ! -s $root/etc/machine-id ]] || fail 'machine-id must be empty'
for name in subuid subgid subuid- subgid-; do
    if [[ -f $root/etc/$name ]] && grep -q '^root:' "$root/etc/$name"; then
        fail "root subordinate ID policy belongs to instance initialization: $name"
    fi
done
# shellcheck disable=SC2016
packages=$(chroot "$root" dpkg-query --show '--showformat=${Package} ${db:Status-Status}\n')
for package in incus-base apparmor apparmor-utils linux-image-arm64 linux-headers-arm64 \
    raspi-firmware firmware-misc-nonfree zfsutils-linux zfs-dkms; do
    awk -v wanted="$package" '$1==wanted && $2=="installed" {found=1} END {exit !found}' \
        <<< "$packages" || fail "required package is not installed: $package"
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
for unit in rsyslog.service qemu-guest-agent.service open-vm-tools.service \
    incus.service incus.socket incus-user.socket incus-user.service incus-startup.service; do
    mask=$root/etc/systemd/system/$unit
    [[ -L $mask && $(readlink -- "$mask") == /dev/null ]] || fail "missing service mask: $unit"
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
# Evaluate actual initramfs configuration in the target, including overrides.
# shellcheck disable=SC2016
chroot "$root" sh -ec '
    . /etc/initramfs-tools/initramfs.conf
    for conf in /etc/initramfs-tools/conf.d/*; do [ ! -f "$conf" ] || . "$conf"; done
    [ "$MODULES" = most ]
' || fail 'initramfs must retain MODULES=most'
kernel_count=0
for modules in "$root"/lib/modules/*; do
    [[ -d $modules ]] || continue
    version=${modules##*/}
    [[ -s $root/boot/vmlinuz-$version && -s $root/boot/initrd.img-$version ]] || fail "missing kernel/initrd: $version"
    vermagic=$(chroot "$root" modinfo -k "$version" -F vermagic zfs)
    [[ $vermagic == "$version "* ]] || fail "ZFS vermagic mismatch: $version"
    listing=$(chroot "$root" lsinitramfs "/boot/initrd.img-$version")
    if ! grep -Fxq "usr/lib/modules/$version/modules.dep" <<< "$listing" &&
       ! grep -Fxq "lib/modules/$version/modules.dep" <<< "$listing"; then
        fail "target modules missing from initrd: $version"
    fi
    grep -qx 'scripts/nfs' <<< "$listing" || fail "NFS startup script missing from initrd: $version"
    nfs_module=$(chroot "$root" modinfo -k "$version" -F filename nfs)
    if [[ $nfs_module != '(builtin)' ]]; then
        nfs_suffix=${nfs_module#*/modules/}
        nfs_suffix=${nfs_suffix%.ko*}.ko
        # initramfs-tools may decompress a module while copying it.
        awk -v wanted="lib/modules/$nfs_suffix" '
            { sub(/\.(xz|zst|gz)$/, ""); sub(/^usr\//, "") }
            $0==wanted {found=1} END {exit !found}
        ' <<< "$listing" || fail "NFS module missing from initrd: $version"
    fi
    kernel_count=$((kernel_count+1))
done
(( kernel_count > 0 )) || fail 'no target kernels found'
python3 "$repository/scripts/raspi-firmware.py" "$root"
printf '[RPI-INCUSD VERIFY] packages, policies, cleanup, ZFS, NFS initramfs and firmware copies passed\n'

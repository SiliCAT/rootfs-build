#!/usr/bin/env bash
set -euo pipefail

[[ $# == 1 && $1 == /* && $1 != / && -d $1 ]] || {
    printf 'Usage: verify-initramfs.sh ABSOLUTE_TARGET_ROOT\n' >&2
    exit 2
}
root=$1
export LC_ALL=C
fail() { printf '[VM-DRACUT VERIFY][ERR] %s\n' "$*" >&2; exit 95; }

required_drivers=(nfs bridge bonding 8021q virtio_net virtio_pci)
other_drivers=(vmxnet3 e1000 e1000e hv_netvsc igb igc ixgbe i40e ice r8169 tg3 bnx2 bnx2x mlx4_en mlx5_core
    usbnet r8152 asix ax88179_178a)

shopt -s nullglob
kernel_count=0
for modules_dir in "$root"/lib/modules/*; do
    [[ -d $modules_dir ]] || continue
    version=${modules_dir##*/}
    initrd=/boot/initrd.img-$version
    [[ -s $root/boot/vmlinuz-$version && -s $root$initrd ]] || fail "missing kernel or initramfs: $version"
    kernel_count=$((kernel_count + 1))

    printf 'Kernel %s\n' "$version"
    modules=$(chroot "$root" lsinitrd -m "$initrd")
    printf '%s\n' "$modules"
    for module in network nfs systemd-networkd kernel-network-modules; do
        grep -Fxq -- "$module" <<< "$modules" || fail "$version: missing dracut module $module"
    done
    for module in network-legacy network-manager connman; do
        if grep -Fxq -- "$module" <<< "$modules"; then
            fail "$version: unexpected networking backend $module"
        fi
    done

    listing=$(chroot "$root" lsinitrd "$initrd")
    paths=$(awk 'NF >= 9 && $1 ~ /^[-ld]/ { print $9 }' <<< "$listing")
    for path in usr/lib/systemd/systemd-networkd usr/lib/systemd/systemd-network-generator \
        usr/lib/systemd/systemd-networkd-wait-online \
        etc/systemd/system/sysinit.target.wants/systemd-network-generator.service \
        etc/systemd/network/zzzz-dracut-default.network.d/10-explicit-ip.conf; do
        grep -Fxq -- "$path" <<< "$paths" || fail "$version: missing $path"
    done
    for tool in ip mount.nfs rpcbind rpc.statd; do
        grep -Eq "(^|/)${tool//./[.]}$" <<< "$paths" || fail "$version: missing $tool"
    done
    if grep -Fxq 'etc/systemd/system/systemd-network-generator.service' <<< "$paths"; then
        fail "$version: rootfs generator mask leaked into the initramfs"
    fi
    # dracut supplies a generic fallback, and may supply unrelated options
    # such as rd.btrfs=0. Check command-line content for boot network settings.
    unexpected=$(grep -E '^etc/systemd/network/.+\.(link|netdev|network)$' <<< "$paths" |
        grep -Fxv 'etc/systemd/network/zzzz-dracut-default.network' || :)
    [[ -z $unexpected ]] || fail "$version: unexpected embedded network configuration: $unexpected"
    while IFS= read -r path; do
        options=$(chroot "$root" lsinitrd -f "$path" "$initrd")
        if grep -Eq '(^|[[:space:]])((root|netroot|nfsroot|ifname|ip|bootdev|bridge|bond|vlan|nameserver|rd\.route)=|rd\.neednet(=|[[:space:]]|$))' <<< "$options"; then
            fail "$version: unexpected embedded boot arguments in $path"
        fi
    done < <(grep -E '^etc/cmdline(\.d/.+\.conf)?$' <<< "$paths")

    printf '\nDriver and firmware coverage\n'
    for driver in "${required_drivers[@]}" "${other_drivers[@]}"; do
        filename=$(chroot "$root" modinfo -k "$version" -F filename "$driver" 2>/dev/null) || filename=
        if [[ $filename == '(builtin)' ]]; then
            printf '%s: built into kernel\n' "$driver"
        elif [[ -n $filename ]] && grep -Fxq -e "lib/modules/${filename#*/lib/modules/}" \
            -e "usr/lib/modules/${filename#*/lib/modules/}" <<< "$paths"; then
            printf '%s: %s\n' "$driver" "$filename"
        else
            if [[ " ${required_drivers[*]} " == *" $driver "* ]]; then
                fail "$version: $driver missing from kernel and initramfs"
            fi
            printf '%s: unavailable in initramfs\n' "$driver"
            continue
        fi

        firmware=$(chroot "$root" modinfo -k "$version" -F firmware "$driver")
        while IFS= read -r file; do
            [[ -n $file ]] || continue
            # Firmware entries can be hardware-specific or globs. Report which
            # alternatives were collected without requiring every board's blob.
            if [[ $file == *'*'* ]]; then
                printf '  firmware %s: wildcard advertised by kernel\n' "$file"
            elif grep -Fxq -e "usr/lib/firmware/$file" -e "usr/lib/firmware/$file.zst" \
                -e "usr/lib/firmware/$file.xz" -e "lib/firmware/$file" \
                -e "lib/firmware/$file.zst" -e "lib/firmware/$file.xz" <<< "$paths"; then
                printf '  firmware %s: included\n' "$file"
            else
                printf '  firmware %s: not included\n' "$file"
            fi
        done <<< "$firmware"
    done
    printf '\n'
done
(( kernel_count > 0 )) || fail 'no target kernels found'

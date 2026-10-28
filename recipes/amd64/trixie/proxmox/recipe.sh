#!/bin/sh
set -eu

# proxmox / Debian Trixie / native amd64 / Incus and guest tools.
# Input: one absolute, real, writable, empty target directory.
# The builder prepares dependencies and native amd64 or binfmt execution first.
# This recipe owns the package set, system configuration and final cleanup.
# It never creates an image, archives the result or mounts host kernel materials.
# The caller must wait for successful completion before validation or archiving.

usage()
{
    cat <<'EOF'
Usage:
  recipe.sh TARGET_ROOT
  recipe.sh --help

Build the proxmox Debian Trixie amd64 rootfs in TARGET_ROOT.
Run as root with mmdebstrap and Debian archive keys already installed.
TARGET_ROOT must be an absolute, real, writable, empty directory.
The recipe accepts no other arguments and does not install builder dependencies.

Success means that mmdebstrap, target-kernel/package checks and final cleanup passed.
The caller owns the build directory, archive, logs and failure recovery.
A failed target directory must not be reused as a successful rootfs.
EOF
}

die()
{
    printf '[PROXMOX][ERR] %s\n' "${1:-unknown error}" >&2
    exit "${2:-1}"
}

need_cmd()
{
    command -v "$1" >/dev/null 2>&1 ||
        die "required command not found: $1" 90
}

if [ "$#" -eq 1 ]; then
    case "$1" in -h|--help) usage; exit 0 ;; esac
fi
[ "$#" -eq 1 ] || die 'expected exactly one TARGET_ROOT argument; see --help' 2
TARGET_ROOT=$1
case "${TARGET_ROOT}" in
    /*) : ;;
    *) die 'TARGET_ROOT must be an absolute path' 2 ;;
esac
while [ "${TARGET_ROOT}" != / ] && [ "${TARGET_ROOT%/}" != "${TARGET_ROOT}" ]; do
    TARGET_ROOT=${TARGET_ROOT%/}
done

need_cmd id
need_cmd realpath
need_cmd find
need_cmd cat
need_cmd install
need_cmd rm
need_cmd truncate
need_cmd chroot
need_cmd cmp

[ "$(id -u)" -eq 0 ] || die 'this recipe must run as root' 1

[ ! -L "${TARGET_ROOT}" ] && [ -d "${TARGET_ROOT}" ] && [ -w "${TARGET_ROOT}" ] ||
    die 'TARGET_ROOT must be a real writable directory' 5
# Preserve trailing newlines in a legal directory name while canonicalizing.
TARGET_RESOLVED=$(realpath -e -- "${TARGET_ROOT}" && printf '.') ||
    die 'cannot resolve TARGET_ROOT' 5
TARGET_ROOT=${TARGET_RESOLVED%?}
TARGET_ROOT=${TARGET_ROOT%"
"}
[ "${TARGET_ROOT}" != / ] || die 'TARGET_ROOT must not resolve to /' 5
TARGET_CONTENTS=$(find "${TARGET_ROOT}" -mindepth 1 -maxdepth 1 -printf x) ||
    die 'cannot inspect TARGET_ROOT' 5
[ -z "${TARGET_CONTENTS}" ] || die 'TARGET_ROOT must be empty, including hidden entries' 5
unset TARGET_CONTENTS TARGET_RESOLVED

# Keep mmdebstrap in the foreground. POSIX sh waits for that command before
# executing a signal trap, so the recipe cannot exit while its build still runs.
# The caller must not remove the target while mmdebstrap is still writing.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

set -x
export DEBIAN_FRONTEND=noninteractive
printf '[PROXMOX 1/3] Checking builder prerequisites\n'
need_cmd mmdebstrap

# Cleanup used by the post-mmdebstrap stage; the chroot hook has its own copy.
clean_tree()
{
    clean_tree_path=$1
    clean_tree_action=$2

    case "${clean_tree_action}" in
        contents|logs|history) : ;;
        *)
            printf "[ERR] unknown cleanup action: %s\n" "${clean_tree_action}" >&2
            return 1
            ;;
    esac

    if [ -L "${clean_tree_path}" ]; then
        printf "[ERR] cleanup directory is a symlink: %s\n" "${clean_tree_path}" >&2
        return 1
    elif [ -d "${clean_tree_path}" ]; then
        :
    elif [ -e "${clean_tree_path}" ]; then
        printf "[ERR] cleanup path is not a directory: %s\n" "${clean_tree_path}" >&2
        return 1
    else
        return 0
    fi

    case "${clean_tree_action}" in
        contents)
            find "${clean_tree_path}" -mindepth 1 -maxdepth 1 \
                -exec rm -rf -- {} +
            ;;
        logs)
            find "${clean_tree_path}" -type f \
                -exec truncate -s 0 -- {} +
            ;;
        history)
            find "${clean_tree_path}" -maxdepth 2 -name .bash_history -type f \
                -exec rm -f -- {} +
            ;;
    esac
}


# Target packages: keep this list in the recipe, one package per line.
PACKAGES='init
systemd
systemd-sysv
dbus
netbase
iproute2
curl
sudo
tzdata
locales
dialog
apt-utils
ca-certificates
openssh-server
nfs-common
ifupdown
systemd-timesyncd
zstd
xz-utils
lz4
bzip2
gzip
iputils-ping
procps
ethtool
rsyslog
logrotate
xfsprogs
btrfs-progs
bcache-tools
lvm2
nbd-client
open-iscsi
qemu-guest-agent
open-vm-tools
systemd-zram-generator
busybox-static
rsync
linux-image-amd64
firmware-realtek
firmware-misc-nonfree
incus-base
apparmor
apparmor-utils'

# Build-only policy values are owned by this recipe, not caller inputs.
# Native --dpkgopt also covers mmdebstrap initial extraction through mmtarfilter.
# Literal APT configuration text, not a shell command or an argument list.
# shellcheck disable=SC2089
PROXMOX_BUILD_APT_CONFIG='Acquire::Languages "none";
APT::Install-Recommends "0";
APT::Install-Suggests "0";'
PROXMOX_BUILD_DPKG_CONFIG='path-exclude=/usr/share/doc/*
path-exclude=/usr/share/man/*
path-exclude=/usr/share/locale/*'
# Export the text verbatim for the hook heredocs.
# shellcheck disable=SC2090
export PROXMOX_BUILD_APT_CONFIG PROXMOX_BUILD_DPKG_CONFIG

printf '[PROXMOX 2/3] 运行 mmdebstrap：%s\n' "${TARGET_ROOT}"

# Hook variables intentionally expand in the child hook shells.
# shellcheck disable=SC2016
mmdebstrap \
  --mode=root \
  --format=directory \
  --variant=minbase \
  --components=main,contrib,non-free,non-free-firmware \
  --include="${PACKAGES}" \
  --dpkgopt="${PROXMOX_BUILD_DPKG_CONFIG}" \
  --setup-hook='
    set -eux
    # A dedicated APT fragment lasts through package configuration and cleanup.
    build_apt="$1/etc/apt/apt.conf.d/90-proxmox-build"
    [ ! -e "${build_apt}" ] && [ ! -L "${build_apt}" ]
    install -D -o 0 -g 0 -m 0644 /dev/stdin "${build_apt}" <<EOF
${PROXMOX_BUILD_APT_CONFIG}
EOF
    # This exact file is generated by the native --dpkgopt above in a fresh root.
    cmp "$1/etc/dpkg/dpkg.cfg.d/99mmdebstrap" /dev/stdin <<EOF
${PROXMOX_BUILD_DPKG_CONFIG}
EOF
  ' \
  --setup-hook='
    set -eux
    # [BUILD] Temporary service-start policy; removed by recipe finalization after mmdebstrap.
    # Block service starts during image build (postinst)
    install -D -o 0 -g 0 -m 0755 /dev/stdin "$1/usr/sbin/policy-rc.d" <<EOF
#!/bin/sh
exit 101
EOF
  ' \
  --setup-hook='
    set -eux
    # [SECURITY] access control / hardening
    install -D -o 0 -g 0 -m 0440 /dev/stdin "$1/etc/sudoers.d/00-sudo-nopasswd" <<EOF
%sudo ALL=(ALL:ALL) NOPASSWD:ALL
EOF

    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/ssh/sshd_config.d/override.conf" <<EOF
PermitRootLogin no
KbdInteractiveAuthentication no
PasswordAuthentication no
#UsePAM no
EOF
  ' \
  --setup-hook='
    set -eux
    # [KERNEL] kernel modules / initramfs boot path
    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/modules-load.d/nbd.conf" <<EOF
nbd
EOF

    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/modules-load.d/overlay.conf" <<EOF
overlay
EOF

    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/modules-load.d/usbip.conf" <<EOF
usbip_core
usbip_host
vhci_hcd
EOF

    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/modprobe.d/99-nbd-max.conf" <<EOF
options nbd nbds_max=1
EOF

    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/initramfs-tools/conf.d/resume" <<EOF
RESUME=none
EOF

    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/systemd/zram-generator.conf.d/10-zram.conf" <<EOF
[zram0]
zram-size = ram / 2
compression-algorithm = zstd
swap-priority = 100
fs-type = swap
EOF
  ' \
  --setup-hook='
    set -eux
    # [SYSTEMD] user-space systemd behavior
    install -D -o 0 -g 0 -m 0644 /dev/stdin "$1/etc/systemd/journald.conf.d/disable.conf" <<EOF
[Journal]
Storage=volatile
ForwardToSyslog=no
Compress=no
EOF
  ' \
  --customize-hook='
    set -eux
    chroot "$1" sh -euxs <<'"'"'EOF'"'"'
export DEBIAN_FRONTEND=noninteractive
sed -i "s/^[# ]*en_US\.UTF-8 UTF-8/en_US.UTF-8 UTF-8/" /etc/locale.gen
dpkg-reconfigure --frontend=noninteractive locales
update-locale LANG=en_US.UTF-8
ln -fs /usr/share/zoneinfo/US/Eastern /etc/localtime
dpkg-reconfigure -f noninteractive tzdata
EOF
  ' \
  --customize-hook='
    set -eux
    chroot "$1" sh -euxs <<'"'"'EOF'"'"'
export DEBIAN_FRONTEND=noninteractive
if ! id -u silicat >/dev/null 2>&1; then
    adduser --uid 1000033 --disabled-password --no-create-home --gecos "" silicat
fi

# Check command results separately: POSIX sh has no portable pipefail.
user_groups=$(id -nG silicat)
case " ${user_groups} " in
    *" sudo "*) : ;;
    *) usermod -a -G sudo silicat ;;
esac

# Keep the uploaded recipe policy: always delete and lock the root password.
# Failure is fatal, just like every other required operation.
passwd -dl root
visudo -cf /etc/sudoers
EOF
  ' \
  --customize-hook='
    set -eux
    # [SYSTEMD] service policy (template defaults): always delete wants first, then mask
    rm -f -- "$1/etc/systemd/system/multi-user.target.wants/rsyslog.service"
    install -d -o 0 -g 0 -m 0755 "$1/etc/systemd/system"
    ln -sf /dev/null "$1/etc/systemd/system/rsyslog.service"

    # Install guest tools but keep their services inactive in the template.
    rm -f -- "$1/etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service"
    ln -sf /dev/null "$1/etc/systemd/system/qemu-guest-agent.service"
    rm -f -- "$1/etc/systemd/system/multi-user.target.wants/open-vm-tools.service"
    ln -sf /dev/null "$1/etc/systemd/system/open-vm-tools.service"

    # Keep Incus inactive; discard its local state in the final cleanup hook.
    ln -sf /dev/null "$1/etc/systemd/system/incus.service"
    ln -sf /dev/null "$1/etc/systemd/system/incus.socket"
    ln -sf /dev/null "$1/etc/systemd/system/incus-user.socket"
    ln -sf /dev/null "$1/etc/systemd/system/incus-user.service"
    ln -sf /dev/null "$1/etc/systemd/system/incus-startup.service"
  ' \
  --customize-hook='
    set -eux
    chroot "$1" sh -euxs <<'"'"'EOF'"'"'
export DEBIAN_FRONTEND=noninteractive

# Cleanup operations used by normal finalization. Missing directories are
# normal; unexpected types/symlinks and command failures are build failures.
# The same function is defined inside the final chroot hook.
clean_tree()
{
    clean_tree_path=$1
    clean_tree_action=$2

    case "${clean_tree_action}" in
        contents|logs|history) : ;;
        *)
            printf "[ERR] unknown cleanup action: %s\n" "${clean_tree_action}" >&2
            return 1
            ;;
    esac

    if [ -L "${clean_tree_path}" ]; then
        printf "[ERR] cleanup directory is a symlink: %s\n" "${clean_tree_path}" >&2
        return 1
    elif [ -d "${clean_tree_path}" ]; then
        :
    elif [ -e "${clean_tree_path}" ]; then
        printf "[ERR] cleanup path is not a directory: %s\n" "${clean_tree_path}" >&2
        return 1
    else
        return 0
    fi

    case "${clean_tree_action}" in
        contents)
            find "${clean_tree_path}" -mindepth 1 -maxdepth 1 \
                -exec rm -rf -- {} +
            ;;
        logs)
            find "${clean_tree_path}" -type f \
                -exec truncate -s 0 -- {} +
            ;;
        history)
            find "${clean_tree_path}" -maxdepth 2 -name .bash_history -type f \
                -exec rm -f -- {} +
            ;;
    esac
}

# Both updates are required and run while policy-rc.d is still present.
update-ca-certificates
apt-get -qq update

# Package cleanup is required; every producer is checked without a pipeline.
apt-get autoremove --purge -y
rc_list=$(mktemp /tmp/rootfs-rc.XXXXXX)
dpkg -l > "${rc_list}"
awk "/^rc/ {print \$2}" "${rc_list}" > "${rc_list}.names"
xargs -r dpkg -P < "${rc_list}.names"
rm -f -- "${rc_list}" "${rc_list}.names"
apt-get clean

# [KERNEL] Validate the final installed kernels, not the runner kernel.
[ "$(dpkg --print-architecture)" = amd64 ]
kernel_count=0
for modules_dir in /lib/modules/*; do
    [ -d "${modules_dir}" ] || continue
    kernel_version=${modules_dir##*/}
    kernel_status=$(dpkg-query -W -f="\${db:Status-Status}" "linux-image-${kernel_version}")
    [ "${kernel_status}" = installed ]
    [ -s "/boot/vmlinuz-${kernel_version}" ]
    [ -s "/boot/initrd.img-${kernel_version}" ]
    kernel_count=$((kernel_count + 1))
done
[ "${kernel_count}" -gt 0 ] || {
    printf "[PROXMOX][ERR] no target kernels found\n" >&2
    exit 95
}

package_audit=$(dpkg --audit)
[ -z "${package_audit}" ] || {
    printf "[PROXMOX][ERR] incomplete package configuration:\n%s\n" "${package_audit}" >&2
    exit 95
}

# All package operations are now finished. Remove service state and identities.
rm -rf -- /var/log/journal /var/lib/incus
rm -f -- /etc/ssh/ssh_host_*
: > /etc/machine-id
rm -f -- /var/lib/dbus/machine-id /var/lib/systemd/random-seed
clean_tree /var/lib/dhcp contents

# Include hidden entries, preserve each top-level directory, propagate failures.
clean_tree /var/lib/apt/lists contents
clean_tree /tmp contents
clean_tree /var/tmp contents
clean_tree /var/log logs
rm -f -- /root/.bash_history
clean_tree /home history
EOF
  ' \
  --customize-hook='
    set -eux
    # All installation, reconfiguration, cleanup and state removal are complete.
    # Compare before deleting: never remove an unrelated/replaced configuration.
    cmp "$1/etc/apt/apt.conf.d/90-proxmox-build" /dev/stdin <<EOF
${PROXMOX_BUILD_APT_CONFIG}
EOF
    cmp "$1/etc/dpkg/dpkg.cfg.d/99mmdebstrap" /dev/stdin <<EOF
${PROXMOX_BUILD_DPKG_CONFIG}
EOF
    rm -- "$1/etc/apt/apt.conf.d/90-proxmox-build" \
        "$1/etc/dpkg/dpkg.cfg.d/99mmdebstrap"
  ' \
  trixie "${TARGET_ROOT}" \
  "deb https://deb.debian.org/debian trixie main contrib non-free non-free-firmware" \
  "deb https://deb.debian.org/debian trixie-updates main contrib non-free non-free-firmware" \
  "deb https://security.debian.org/debian-security trixie-security main contrib non-free non-free-firmware" \
  "deb https://deb.debian.org/debian trixie-backports main contrib non-free non-free-firmware" \
  "deb-src https://deb.debian.org/debian trixie-backports main contrib non-free non-free-firmware"

# ---------------------------------------------------------------------------
# 配方收尾：mmdebstrap 成功返回之后执行
# ---------------------------------------------------------------------------

echo "[PROXMOX 3/3] 严格配方收尾（构建期策略、唯一性、日志、history）"

# Wait until mmdebstrap has finished its own service-policy handling.
rm -f -- "${TARGET_ROOT}/usr/sbin/policy-rc.d"
if [ -e "${TARGET_ROOT}/usr/sbin/policy-rc.d" ] || \
   [ -L "${TARGET_ROOT}/usr/sbin/policy-rc.d" ]; then
    die "build-time policy-rc.d remains in the rootfs" 95
fi

for build_path in etc/apt/apt.conf.d/90-proxmox-build \
    etc/dpkg/dpkg.cfg.d/99mmdebstrap etc/build-meta; do
    if [ -e "${TARGET_ROOT}/${build_path}" ] || [ -L "${TARGET_ROOT}/${build_path}" ]; then
        die "unexpected build-only file remains: ${build_path}" 95
    fi
done

install -D -o 0 -g 0 -m 0644 /dev/null \
    "${TARGET_ROOT}/etc/machine-id"
rm -f -- "${TARGET_ROOT}/var/lib/dbus/machine-id" \
    "${TARGET_ROOT}/var/lib/systemd/random-seed"
clean_tree "${TARGET_ROOT}/var/lib/dhcp" contents
clean_tree "${TARGET_ROOT}/var/log" logs
rm -f -- "${TARGET_ROOT}/root/.bash_history"
clean_tree "${TARGET_ROOT}/home" history

# The caller validates mount cleanup and archives only after successful return.
# No runner module loading, image operations or archive creation belongs here.
printf '[PROXMOX][DONE] recipe ready: %s\n' "${TARGET_ROOT}"
exit 0

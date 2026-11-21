#!/bin/sh
set -eu

usage()
{
    cat <<'EOF'
Usage:
  recipe.sh TARGET_ROOT
  recipe.sh --help

Build the Debian Trixie amd64 schroot maintenance rootfs.
Run as root with mmdebstrap and Debian archive keys installed.
TARGET_ROOT must be an absolute, real, writable, empty directory.
EOF
}

die()
{
    printf '[SCHROOT-MAINTENANCE][ERR] %s\n' "${1:-unknown error}" >&2
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
need_cmd chroot
need_cmd install
need_cmd mmdebstrap

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

# APT, archives, networking, partitioning, filesystem tools, storage and diagnostics.
PACKAGES='apt
sudo
debian-archive-keyring
ca-certificates
tzdata
locales
bzip2
xz-utils
zstd
lz4
lzop
lzip
zip
unzip
7zip
cpio
curl
wget
iproute2
iputils-ping
bind9-dnsutils
netcat-openbsd
incus-client
parted
fdisk
kpartx
util-linux
util-linux-extra
file
e2fsprogs
dosfstools
mtools
exfatprogs
xfsprogs
btrfs-progs
f2fs-tools
squashfs-tools
erofs-utils
ntfs-3g
qemu-utils
smartmontools
nvme-cli
mdadm
cryptsetup-bin
nbd-client
open-iscsi
nfs-common
procps
psmisc
lsof'

export DEBIAN_FRONTEND=noninteractive
components='main contrib non-free non-free-firmware'
signing='[signed-by=/usr/share/keyrings/debian-archive-keyring.gpg]'
archive='https://deb.debian.org/debian'
security='https://security.debian.org/debian-security'

# mmdebstrap skips Recommends by default. Keep only the translation setting
# in the APT config of the delivered rootfs.
# The hook arguments are intentionally expanded in the hook shell.
# shellcheck disable=SC2016
mmdebstrap \
    --mode=root \
    --format=directory \
    --architectures=amd64 \
    --variant=minbase \
    --components=main,contrib,non-free,non-free-firmware \
    --include="${PACKAGES}" \
    --aptopt='Acquire::Languages "none"' \
    --dpkgopt='path-exclude=/usr/share/doc/*' \
    --dpkgopt='path-include=/usr/share/doc/*/copyright' \
    --dpkgopt='path-exclude=/usr/share/man/*' \
    --dpkgopt='path-exclude=/usr/share/groff/*' \
    --dpkgopt='path-exclude=/usr/share/info/*' \
    --dpkgopt='path-exclude=/usr/share/lintian/*' \
    --dpkgopt='path-exclude=/usr/share/linda/*' \
    --dpkgopt='path-exclude=/usr/share/locale/*' \
    --dpkgopt='path-include=/usr/share/locale/locale.alias' \
    --setup-hook='
        set -eu
        install -D -o 0 -g 0 -m 0755 /dev/stdin "$1/usr/sbin/policy-rc.d" <<EOF_POLICY
#!/bin/sh
exit 101
EOF_POLICY

    install -D -o 0 -g 0 -m 0440 /dev/stdin "$1/etc/sudoers.d/00-sudo-nopasswd" <<EOF
%sudo ALL=(ALL:ALL) NOPASSWD:ALL
EOF
    ' \
    --customize-hook='
        set -eu
        chroot "$1" sh -es <<'"'"'EOF_CHROOT'"'"'
export DEBIAN_FRONTEND=noninteractive
# [LOCALE] Generate and configure the target locale.
sed -i "s/^[# ]*en_US\.UTF-8 UTF-8$/en_US.UTF-8 UTF-8/" /etc/locale.gen
dpkg-reconfigure --frontend=noninteractive locales
update-locale --reset LANG=en_US.UTF-8

# [TIMEZONE] Use the canonical timezone identifier.
ln -sfn /usr/share/zoneinfo/America/New_York /etc/localtime
dpkg-reconfigure -f noninteractive tzdata

passwd -dl root
visudo -cf /etc/sudoers

# Verify the shipped sources using the target CA and archive keyrings.
apt-get --error-on=any update
apt-get clean

rm -f /etc/dpkg/dpkg.cfg.d/99mmdebstrap
install -o 0 -g 0 -m 0644 /dev/null /etc/machine-id
rm -f /var/lib/dbus/machine-id /var/lib/systemd/random-seed
for directory in /var/lib/apt/lists /tmp /var/tmp; do
    find "$directory" -xdev -mindepth 1 -delete
done
find /var/log -xdev -type f -exec truncate -s 0 -- {} +
EOF_CHROOT
    ' \
    trixie "${TARGET_ROOT}" \
    "deb $signing $archive trixie $components" \
    "deb-src $signing $archive trixie $components" \
    "deb $signing $archive trixie-updates $components" \
    "deb-src $signing $archive trixie-updates $components" \
    "deb $signing $security trixie-security $components" \
    "deb-src $signing $security trixie-security $components" \
    "deb $signing $archive trixie-backports $components" \
    "deb-src $signing $archive trixie-backports $components"

# Install the runtime policy after mmdebstrap restores its build-time policy.
install -D -o 0 -g 0 -m 0755 /dev/stdin "${TARGET_ROOT}/usr/sbin/policy-rc.d" <<'EOF_POLICY'
#!/bin/sh
exit 101
EOF_POLICY
printf '[SCHROOT-MAINTENANCE][DONE] recipe ready: %s\n' "${TARGET_ROOT}"

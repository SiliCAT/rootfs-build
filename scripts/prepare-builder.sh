#!/usr/bin/env bash
set -euo pipefail

[[ $# == 0 ]] || { printf 'Usage: prepare-builder.sh\n' >&2; exit 2; }
[[ $(id -u) == 0 ]] || { printf 'Builder preparation requires root\n' >&2; exit 1; }
# This script runs inside the selected Debian builder, never on the Ubuntu host.
# shellcheck source=/dev/null
source /etc/os-release
[[ $ID == debian && ${VERSION_CODENAME:-} == trixie ]] || {
    printf 'Expected a Debian Trixie builder\n' >&2
    exit 1
}
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    bash coreutils findutils grep sed mawk diffutils tar zstd xz-utils gzip bzip2 \
    util-linux procps mmdebstrap debian-archive-keyring ca-certificates \
    python3 acl attr libcap2-bin
update-ca-certificates

unshare --mount -- true
chroot / /bin/true
printf '[BUILDER] architecture=%s available_cpus=%s\n' "$(dpkg --print-architecture)" "$(nproc)"
mmdebstrap --version
tar --version | head -n 1
zstd --version
df -h -- . /tmp

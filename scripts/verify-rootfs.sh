#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 || $1 != /* ]]; then
    printf 'Usage: verify-rootfs.sh ABSOLUTE_TARGET_ROOT\n' >&2
    exit 2
fi

root=$1
while [[ $root != / && $root == */ ]]; do root=${root%/}; done
if [[ $root == / || -L $root || ! -d $root ]]; then
    printf '[VERIFY][ERR] target must be a real directory other than /\n' >&2
    exit 5
fi
root=$(realpath -e -- "$root" && printf '.')
root=${root%.}
root=${root%$'\n'}
if [[ $root == / ]]; then
    printf '[VERIFY][ERR] refusing the host root directory\n' >&2
    exit 5
fi

# Reject mounts at the root itself or anywhere below it, including bind mounts
# on the same device. --one-file-system alone cannot detect those bind mounts.
MOUNT_QUERY_PATH=$root awk '
function unescape(s,    i,e,c,r) {
    r=""
    for (i=1; i<=length(s); i++) {
        c=substr(s,i,1)
        if (c=="\\" && i+3<=length(s)) {
            e=substr(s,i+1,3)
            if (e=="040") { c=" "; i+=3 }
            else if (e=="011") { c=sprintf("%c",9); i+=3 }
            else if (e=="012") { c=sprintf("%c",10); i+=3 }
            else if (e=="134") { c="\\"; i+=3 }
        }
        r=r c
    }
    return r
}
BEGIN { target=ENVIRON["MOUNT_QUERY_PATH"]; count=0; bad=0 }
{
    count++
    separator=0
    for (j=7;j<=NF;j++) if ($j=="-") { separator=j; break }
    if (NF<10 || $1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ ||
        !separator || separator+3>NF) { bad=1; next }
    path=unescape($5)
    if (path==target || index(path,target "/")==1) {
        print "[VERIFY][ERR] residual mount: " path > "/dev/stderr"
        bad=1
    }
}
END { if (bad || count==0) exit 95 }
' /proc/self/mountinfo

# Only structural checks belong here. Device, package, kernel and service policy
# checks belong to the recipe or its optional sibling verify.sh.
for directory in etc usr var; do
    if [[ -L $root/$directory || ! -d $root/$directory ]]; then
        printf '[VERIFY][ERR] missing real rootfs directory: %s\n' "$directory" >&2
        exit 95
    fi
done
printf '[VERIFY] rootfs structure and mount cleanup passed\n'

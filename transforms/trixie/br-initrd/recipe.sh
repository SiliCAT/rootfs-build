#!/bin/bash
# Helpers are serialized into chroots; quoted commands expand in the child.
# shellcheck disable=SC2016,SC2329
set -Eeuo pipefail

# Shared Debian Trixie rootfs transform. Keep bridge policy in this file.
# The build shell owns the empty target, mount namespace and output archive.
# This recipe owns extraction, chroot views, injection and read-back acceptance.

usage() {
    cat <<'HELP'
Usage:
  recipe.sh TARGET_ROOT INPUT_ROOTFS.tar.zst
  recipe.sh --help

Run as root through scripts/build-rootfs.sh --input-rootfs INPUT_ROOTFS.tar.zst.
TARGET_ROOT must be absolute and empty. Input contains etc/, usr/, var/ directly.
Use a native builder matching the input architecture; this recipe has no CPU
architecture branches. Debian initramfs-tools and the target kernel are required.
Optional BR_INITRD_REPORT: absolute external path for the acceptance JSON record.

Runtime bridge selection:
  br_exp=1 ip=CLIENT:SERVER:GATEWAY:MASK:HOST:BRIDGE:AUTOCONF
  br_if_mac=aa:bb:cc:dd:ee:ff takes precedence over br_if=NAME and AUTO.
  An explicitly provided MAC never falls back on failure (including empty MAC).
  MAC mode matches the current sysfs MAC; up to 11 scans / ten 1-second waits.
HELP
}
STAGE=PRECHECK
emit() { local level=$1; shift; printf '[BR-INJECT][%s][%s] %s\n' "${STAGE:-UNKNOWN}" "$level" "$*"; }
log() { emit INFO "$*"; }
warn() { emit WARN "$*" >&2; }
die() { emit ERR "$1" >&2; exit "${2:-1}"; }
step() { STAGE=$1; shift; emit ACT "$*"; }
# Selective command tracing: no global set -x, no expanded initramfs listings.
run() {
    printf '[BR-INJECT][%s][TRACE]' "${STAGE:-UNKNOWN}" >&2
    printf ' %q' "$@" >&2
    printf '\n' >&2
    "$@"
}
on_error() {
    local rc=$1 line=$2 command=$3
    printf '[BR-INJECT][%s][ERR] rc=%s line=%s command=%q\n' \
        "${STAGE:-UNKNOWN}" "$rc" "$line" "$command" >&2
}
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1" 90; }
check() {
    local description=$1 rc; shift
    if "$@"; then emit OK "$description"; else
        rc=$?; emit ERR "$description (rc=$rc)" >&2; return "$rc"
    fi
}
sha_file() { local value; value=$(sha256sum < "$1") || return; printf '%s' "${value%% *}"; }
report_file() {
    local label=$1 path=$2 metadata checksum quoted
    [[ -f $path ]] || die "$label is not a regular file: $path" 95
    metadata=$(stat -Lc 'mode=%a uid=%u gid=%g bytes=%s' -- "$path")
    checksum=$(sha_file "$path")
    printf -v quoted '%q' "$path"
    emit OK "$label: path=$quoted $metadata sha256=$checksum"
}
absent() { [[ ! -e $1 && ! -L $1 ]]; }
remove_paths() {
    local label=$1 path; shift
    local -a present=()
    for path in "$@"; do
        if [[ -e $path || -L $path ]]; then present+=("$path"); fi
    done
    if [[ ${#present[@]} == 0 ]]; then emit SKIP "$label: already absent"; return 0; fi
    emit ACT "$label: remove ${#present[@]} item(s)"
    run rm -f -- "${present[@]}"
    for path in "${present[@]}"; do check "removed: $path" absent "$path"; done
}
count_find() { find "$@" -printf 'x\n' | awk 'END {print NR+0}'; }
clean_tree() {
    local path=$1 action=$2 before after nonempty
    [[ ! -L $path ]] || die "cleanup directory is a symlink: $path" 95
    if [[ ! -e $path ]]; then emit SKIP "cleanup $action: directory absent: $path"; return 0; fi
    [[ -d $path ]] || die "cleanup path is not a directory: $path" 95
    case $action in
        contents)
            before=$(count_find "$path" -mindepth 1 -maxdepth 1)
            emit ACT "cleanup contents: $path; top-level entries before=$before (includes hidden entries)"
            run find "$path" -mindepth 1 -maxdepth 1 -exec rm -rf -- '{}' +
            after=$(count_find "$path" -mindepth 1 -maxdepth 1)
            [[ $after == 0 ]] || die "cleanup left $after entries: $path" 95
            emit OK "cleanup contents: $path; before=$before after=$after; directory retained"
            ;;
        logs)
            before=$(count_find "$path" -type f)
            nonempty=$(count_find "$path" -type f -size +0c)
            emit ACT "truncate logs: $path; files=$before nonempty_before=$nonempty"
            run find "$path" -type f -exec truncate -s 0 -- '{}' +
            after=$(count_find "$path" -type f -size +0c)
            [[ $after == 0 ]] || die "nonempty log files remain: $path" 95
            emit OK "truncate logs: $path; files=$before nonempty_after=$after"
            ;;
        history)
            before=$(count_find "$path" -maxdepth 2 -name .bash_history -type f)
            emit ACT "remove shell history: $path; matches_before=$before; maxdepth=2"
            run find "$path" -maxdepth 2 -name .bash_history -type f -exec rm -f -- '{}' +
            after=$(count_find "$path" -maxdepth 2 -name .bash_history -type f)
            [[ $after == 0 ]] || die "shell history remains: $path" 95
            emit OK "shell history: $path; matches_before=$before after=$after"
            ;;
        *) die "unknown cleanup action: $action" 95 ;;
    esac
}

trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR

# Send logging helpers and a strict execution context to each independent chroot.
emit_chroot() {
    printf 'set -Eeuo pipefail\n'
    declare -f emit log warn die step run on_error need_cmd check sha_file report_file absent remove_paths count_find clean_tree
    printf 'STAGE=%q\n' "$1"
    printf 'trap %q ERR\n' 'on_error "$?" "$LINENO" "$BASH_COMMAND"'
}

if [[ $# == 1 && ( $1 == --help || $1 == -h ) ]]; then usage; exit 0; fi
[[ $# == 2 ]] || die 'expected TARGET_ROOT and TARBALL; see --help' 2
[[ $1 == /* ]] || die 'TARGET_ROOT must be absolute' 2
for command in id realpath find tar install cat awk grep chmod mount umount \
               chroot mktemp rm rmdir bash sh sha256sum stat cmp cp readlink; do need_cmd "$command"; done
[[ $(id -u) == 0 ]] || die 'run the entire recipe as root' 1
[[ -d $1 && ! -L $1 && -w $1 ]] || die 'TARGET_ROOT must be a real writable directory' 5
IFS= read -r -d '' DST_ROOT < <(realpath -ze -- "$1") || die 'cannot resolve TARGET_ROOT' 5
[[ $DST_ROOT != / ]] || die 'TARGET_ROOT must not be /' 5
[[ -f $2 && -r $2 ]] || die "tarball is not a readable regular file: $2" 2
IFS= read -r -d '' TARBALL < <(realpath -ze -- "$2") || die 'cannot resolve TARBALL' 2
IFS= read -r -d '' SELF < <(realpath -ze -- "${BASH_SOURCE[0]}") || die 'cannot resolve recipe path' 5
[[ $TARBALL != "$DST_ROOT/"* && $SELF != "$DST_ROOT/"* ]] || die 'inputs must be outside TARGET_ROOT' 5
CONTENTS=$(find "$DST_ROOT" -mindepth 1 -maxdepth 1 -printf x) || die 'cannot inspect TARGET_ROOT' 5
[[ -z $CONTENTS ]] || die 'TARGET_ROOT must be empty, including hidden entries' 5
unset CONTENTS
[[ -r /proc/self/mountinfo ]] || die '/proc/self/mountinfo must be readable' 90

# VIEW_ID is '-' when absent. Refuse stacked mounts or an unreadable table.
# All paths passed here are canonical; decode kernel mountinfo escapes first.
inspect_view() {
    local report
    report=$(BR_VIEW_PATH="$1" awk '
    function dec(s) {
        gsub(/\\040/," ",s); gsub(/\\011/,"\t",s)
        gsub(/\\012/,"\n",s); gsub(/\\134/,sprintf("%c",92),s); return s
    }
    BEGIN { wanted=ENVIRON["BR_VIEW_PATH"]; count=0; children=0; id="-"; valid=0 }
    NF>=10 && / - / {
        p=dec($5); if(p=="/") valid=1
        if(p==wanted) {count++; id=$1}
        else if(index(p,wanted "/")==1) children++
    }
    END { if(!valid || count>1) exit 1; print id,children }
    ' /proc/self/mountinfo) || return 1
    read -r VIEW_ID VIEW_CHILDREN <<< "$report"
    [[ $VIEW_ID == - || $VIEW_ID =~ ^[0-9]+$ ]] && [[ $VIEW_CHILDREN =~ ^[0-9]+$ ]]
}

VIEW_PATHS=("$DST_ROOT/dev" "$DST_ROOT/proc" "$DST_ROOT/sys")
VIEW_IDS=('' '' '')
VIEW_ATTEMPTED=(0 0 0)
STATE_DIR=
CONTEXT_READY=0
restore_context() {
    [[ $CONTEXT_READY == 1 ]] || return 0
    local name target
    for name in resolv.conf policy-rc.d; do
        case $name in
            resolv.conf) target=$DST_ROOT/etc/resolv.conf ;;
            policy-rc.d) target=$DST_ROOT/usr/sbin/policy-rc.d ;;
        esac
        rm -f -- "$target" || return
        if [[ -e $STATE_DIR/$name || -L $STATE_DIR/$name ]]; then
            cp -a -- "$STATE_DIR/$name" "$target" || return
        fi
    done
    CONTEXT_READY=0
}
for path in "${VIEW_PATHS[@]}"; do
    inspect_view "$path" || die "cannot inspect view before extraction: $path" 5
    [[ $VIEW_ID == - && $VIEW_CHILDREN == 0 ]] || die "pre-existing mount under target view: $path" 5
done

release_views() {
    local index failed=0
    for index in 2 1 0; do
        [[ ${VIEW_ATTEMPTED[index]} == 1 ]] || continue
        if ! inspect_view "${VIEW_PATHS[index]}"; then
            warn "cannot inspect view; retaining it: ${VIEW_PATHS[index]}"; failed=1; continue
        fi
        if [[ $VIEW_ID == - && $VIEW_CHILDREN == 0 ]]; then
            VIEW_ATTEMPTED[index]=0; continue
        fi
        if [[ -z ${VIEW_IDS[index]} || $VIEW_ID != "${VIEW_IDS[index]}" ]]; then
            warn "view ownership cannot be confirmed; retaining: ${VIEW_PATHS[index]}"; failed=1; continue
        fi
        if [[ $VIEW_CHILDREN != 0 ]]; then
            warn "unexpected child mounts; retaining view: ${VIEW_PATHS[index]}"; failed=1; continue
        fi
        emit ACT "unmount owned view: ${VIEW_PATHS[index]}; mount_id=${VIEW_IDS[index]}"
        if ! run umount -- "${VIEW_PATHS[index]}"; then
            warn "cannot unmount owned view: ${VIEW_PATHS[index]}"; failed=1; continue
        fi
        if inspect_view "${VIEW_PATHS[index]}" && [[ $VIEW_ID == - && $VIEW_CHILDREN == 0 ]]; then
            VIEW_ATTEMPTED[index]=0
            emit OK "view released and absence confirmed: ${VIEW_PATHS[index]}"
        else
            warn "cannot confirm view removal: ${VIEW_PATHS[index]}"; failed=1
        fi
    done
    return "$failed"
}
cleanup() {
    local rc=$? cleanup_failed=0
    trap - EXIT ERR
    trap '' HUP INT TERM
    set +e
    emit ACT "exit cleanup: original_rc=$rc"
    restore_context || cleanup_failed=1
    release_views || cleanup_failed=1
    if [[ -n $STATE_DIR ]]; then
        if [[ ${VIEW_ATTEMPTED[*]} == '0 0 0' ]]; then
            if [[ $cleanup_failed == 0 ]]; then
                if ! rm -rf -- "$STATE_DIR"; then warn "runtime directory retained: $STATE_DIR"; cleanup_failed=1; fi
            else
                warn "context recovery failed; runtime directory retained: $STATE_DIR"
            fi
        else
            warn "view resources remain; runtime directory retained: $STATE_DIR"
            cleanup_failed=1
        fi
    fi
    if [[ $rc == 0 && $cleanup_failed != 0 ]]; then rc=1; fi
    exit "$rc"
}
# Foreground commands finish before Bash executes these signal traps. There is
# no background writer left behind when cleanup starts. For a long command the
# outer shell may time out its stop request and preserve the enclosing image.
trap cleanup EXIT
trap 'emit WARN "received HUP; exiting after foreground command"; exit 129' HUP
trap 'emit WARN "received INT; exiting after foreground command"; exit 130' INT
trap 'emit WARN "received TERM; exiting after foreground command"; exit 143' TERM
STATE_DIR=$(mktemp -d /tmp/br-initrd-recipe.XXXXXXXX)
export DEBIAN_FRONTEND=noninteractive

step S1 "extracting tarball=$TARBALL; target=$DST_ROOT"
archive_bytes=$(stat -Lc %s -- "$TARBALL")
emit INFO "input archive bytes=$archive_bytes; target is the supplied system root (no extra rootfs layer)"
extract_start=$SECONDS
run env -u TAR_OPTIONS -u POSIXLY_CORRECT tar --extract --file="$TARBALL" \
    --numeric-owner --same-owner --same-permissions --acls --xattrs \
    --xattrs-include='*' --selinux --delay-directory-restore --directory="$DST_ROOT"
run env -u TAR_OPTIONS -u POSIXLY_CORRECT tar --compare --file="$TARBALL" \
    --numeric-owner --acls --xattrs --xattrs-include='*' --selinux --directory="$DST_ROOT"
# A pack-produced archive contains the system contents, not another rootfs/.
[[ -d $DST_ROOT/etc && ! -L $DST_ROOT/etc && -d $DST_ROOT/usr && ! -L $DST_ROOT/usr ]] ||
    die 'archive must contain etc/ and usr/ directly; do not wrap them in rootfs/' 5
[[ -x $DST_ROOT/bin/bash ]] || die 'target /bin/bash is required' 90
emit OK "archive extracted; etc/, usr/ and /bin/bash checked; elapsed=$((SECONDS-extract_start))s"
for directory in dev proc sys; do
    [[ ! -L $DST_ROOT/$directory ]] || die "target view must not be a symlink: $directory" 5
    if [[ -e $DST_ROOT/$directory && ! -d $DST_ROOT/$directory ]]; then die "target view is not a directory: $directory" 5; fi
    install -d -o 0 -g 0 -m 0755 -- "$DST_ROOT/$directory"
done

# Validate the target before writing hooks or invoking package operations.
[[ $(chroot "$DST_ROOT" dpkg --print-architecture) == "$(dpkg --print-architecture)" ]] ||
    die 'input architecture must match the native Debian builder' 95
run chroot "$DST_ROOT" /bin/bash -Eeuo pipefail -c '
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin
    . /etc/os-release
    [[ $ID == debian && ${VERSION_CODENAME:-} == trixie ]]
    for tool in update-initramfs unmkinitramfs modinfo ip sleep true update-ca-certificates apt-get dpkg; do
        command -v "$tool" >/dev/null || exit 90
    done
    [[ -d /lib/modules && -d /boot ]]
'
REPORT_PATH=${BR_INITRD_REPORT:-}
if [[ -n $REPORT_PATH ]]; then
    need_cmd python3
    [[ $REPORT_PATH == /* ]] || die 'BR_INITRD_REPORT must be absolute' 5
    IFS= read -r -d '' REPORT_PATH < <(realpath -zm -- "$REPORT_PATH") ||
        die 'cannot resolve BR_INITRD_REPORT' 5
    [[ $REPORT_PATH == /* && $REPORT_PATH != "$DST_ROOT" && $REPORT_PATH != "$DST_ROOT/"* ]] ||
        die 'BR_INITRD_REPORT must be an absolute path outside TARGET_ROOT' 5
    [[ ! -e $REPORT_PATH && ! -L $REPORT_PATH ]] || die 'acceptance report already exists' 5
fi

step S2 'installing bridge payload and initramfs tools hook'
HOOK_DIR="$DST_ROOT/etc/initramfs-tools/hooks"
PREMOUNT_DIR="$DST_ROOT/etc/initramfs-tools/scripts/init-premount"
install -d -o 0 -g 0 -m 0755 -- "$HOOK_DIR" "$PREMOUNT_DIR"
awk '
    $0=="#===PAYLOAD:10-br-exp:BEGIN===" {if(++begins!=1 || ends) bad=1; active=1; next}
    $0=="#===PAYLOAD:10-br-exp:END===" {if(!active || ++ends!=1) bad=1; active=0; next}
    active {if(index($0,"\r")) bad=1; print}
    END {if(begins!=1 || ends!=1 || active || bad) exit 1}
' "$SELF" > "$STATE_DIR/payload" || die 'missing, duplicated or CRLF bridge payload' 5
run sh -n "$STATE_DIR/payload"
run install -o 0 -g 0 -m 0755 -- "$STATE_DIR/payload" "$PREMOUNT_DIR/10-br-exp"

check 'installed payload equals extracted source' cmp -s -- "$STATE_DIR/payload" "$PREMOUNT_DIR/10-br-exp"
report_file 'installed MAC-capable bridge script' "$PREMOUNT_DIR/10-br-exp"
cat > "$HOOK_DIR/20-iproute2" <<'HOOK'
#!/bin/sh
set -eu
case "${1:-}" in prereqs) echo ''; exit 0 ;; esac
. /usr/share/initramfs-tools/hook-functions
hook_log() { printf '[BR-INJECT][HOOK][%s] %s\n' "$1" "$2" >&2; }
# Shared by the build hook and verifier: fixed search order, then canonicalize.
# In particular, do not let differing build/chroot PATHs select different ip's.
select_tool_source() {
    for tool_dir in /usr/bin /bin /usr/sbin /sbin; do
        if [ -f "$tool_dir/$1" ] && [ -x "$tool_dir/$1" ]; then
            readlink -f -- "$tool_dir/$1"
            return
        fi
    done
    tool_fallback=$(command -v "$1") || return 1
    [ -f "$tool_fallback" ] && [ -x "$tool_fallback" ] || return 1
    readlink -f -- "$tool_fallback"
}
dest_real=$(readlink -f -- "$DESTDIR")
mkdir -p "$DESTDIR/usr/lib/br-exp"
private_dir=$(readlink -f -- "$DESTDIR/usr/lib/br-exp")
case "$private_dir/" in "$dest_real/"*) : ;; *) echo 'E: br-exp private tool directory escapes initramfs' >&2; exit 1 ;; esac

# copy_exec does not overwrite existing destinations. Copy each selected tool
# under a private, collision-free name, then replace only the public directory
# entries with relative links. Never write through a BusyBox applet symlink.
link_tool() {
    link_name=$1
    for logical_dir in usr/bin usr/sbin bin sbin; do
        mkdir -p "$DESTDIR/$logical_dir"
        link_parent=$(readlink -f -- "$DESTDIR/$logical_dir")
        case "$link_parent/" in "$dest_real/"*) : ;; *) echo "E: br-exp tool alias parent escapes initramfs: $logical_dir" >&2; exit 1 ;; esac
        ln -sfrT -- "$private_dir/$link_name" "$link_parent/$link_name"
        hook_log OK "tool entry /$logical_dir/$link_name -> /usr/lib/br-exp/$link_name"
    done
}
copy_tool() {
    copy_source=$1
    copy_name=$2
    hook_log ACT "copy_exec source=$copy_source private=/usr/lib/br-exp/$copy_name"
    copy_exec "$copy_source" "/usr/lib/br-exp/$copy_name"
    if ! cmp -s -- "$copy_source" "$private_dir/$copy_name"; then
        echo "E: br-exp private copy differs from source: $copy_name" >&2
        exit 1
    fi
    link_tool "$copy_name"
    hook_log OK "copied and byte-checked tool: $copy_name"
}
ip_path=$(select_tool_source ip) || { echo 'E: br-exp requires ip' >&2; exit 1; }
sleep_path=$(select_tool_source sleep) || { echo 'E: br-exp MAC mode requires sleep' >&2; exit 1; }
true_path=$(select_tool_source true) || { echo 'E: br-exp requires external true' >&2; exit 1; }
copy_tool "$ip_path" ip
copy_tool "$sleep_path" sleep
copy_tool "$true_path" true
if udhcpc_path=$(select_tool_source udhcpc); then
    hook_log INFO "DHCP branch=native source=$udhcpc_path"
    copy_tool "$udhcpc_path" udhcpc
elif busybox_path=$(select_tool_source busybox) && "$busybox_path" udhcpc --help >/dev/null 2>&1; then
    hook_log INFO "DHCP branch=BusyBox-wrapper source=$busybox_path"
    copy_tool "$busybox_path" br-exp-busybox
    [ ! -L "$private_dir/udhcpc" ] || { echo 'E: unexpected symlink at private udhcpc wrapper' >&2; exit 1; }
    cat > "$private_dir/udhcpc" <<'UDHCPC'
#!/bin/sh
exec /bin/br-exp-busybox udhcpc "$@"
UDHCPC
    chmod 0755 "$private_dir/udhcpc"
    link_tool udhcpc
    hook_log OK 'installed private udhcpc wrapper and relative public entries'
else
    hook_log WARN 'no DHCP client copied; manual/MAC modes remain usable, AUTO requires udhcpc'
fi
HOOK
run chmod 0755 "$HOOK_DIR/20-iproute2"
run sh -n "$HOOK_DIR/20-iproute2"
report_file 'installed initramfs tools hook' "$HOOK_DIR/20-iproute2"
MODFILE="$DST_ROOT/etc/initramfs-tools/modules"
[[ -e $MODFILE ]] || install -o 0 -g 0 -m 0644 /dev/null "$MODFILE"
for module in bridge llc stp; do
    if grep -qx -- "$module" "$MODFILE"; then emit SKIP "module already requested: $module"; else
        grep_rc=$?
        [[ $grep_rc == 1 ]] || die "cannot read modules file: $MODFILE" 5
        printf '%s\n' "$module" >> "$MODFILE"
        emit OK "module request appended: $module; file=$MODFILE"
    fi
done

report_file 'module request file' "$MODFILE"
step S3 'mounting owned chroot views (dev/proc/sys)'
for index in 0 1 2; do
    inspect_view "${VIEW_PATHS[index]}" || die 'cannot inspect view before mounting' 5
    [[ $VIEW_ID == - && $VIEW_CHILDREN == 0 ]] || die "target view became occupied: ${VIEW_PATHS[index]}" 5
    VIEW_ATTEMPTED[index]=1
    case $index in
        0) run mount --bind /dev "${VIEW_PATHS[index]}" ;;
        1) run mount -t proc proc "${VIEW_PATHS[index]}" ;;
        2) run mount -t sysfs sys "${VIEW_PATHS[index]}" ;;
    esac
    inspect_view "${VIEW_PATHS[index]}" || die 'cannot identify newly mounted view' 5
    [[ $VIEW_ID != - && $VIEW_CHILDREN == 0 ]] || die 'new view mount is missing or has unexpected children' 5
    VIEW_IDS[index]=$VIEW_ID
    run mount --make-private "${VIEW_PATHS[index]}"
    emit OK "view mounted and private: ${VIEW_PATHS[index]}; mount_id=${VIEW_IDS[index]}"
done

# Package operations must not start services, and require working build DNS.
# Preserve regular files and symlinks (including dangling links) exactly.
for name in resolv.conf policy-rc.d; do
    case $name in
        resolv.conf) target=$DST_ROOT/etc/resolv.conf ;;
        policy-rc.d) target=$DST_ROOT/usr/sbin/policy-rc.d ;;
    esac
    if [[ -e $target || -L $target ]]; then cp -a -- "$target" "$STATE_DIR/$name"; fi
done
CONTEXT_READY=1
rm -f -- "$DST_ROOT/etc/resolv.conf" "$DST_ROOT/usr/sbin/policy-rc.d"
install -o 0 -g 0 -m 0644 -- "$(readlink -e /etc/resolv.conf)" "$DST_ROOT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$DST_ROOT/usr/sbin/policy-rc.d"
chmod 0755 "$DST_ROOT/usr/sbin/policy-rc.d"

declare -A ORIGINAL_IMAGES=()
shopt -s nullglob
for image in "$DST_ROOT"/boot/initrd.img-*; do
    [[ -f $image ]] || continue
    ORIGINAL_IMAGES["${image#"$DST_ROOT"}"]=$(stat -Lc '%i:%s:%y:%z' -- "$image")
done

step S4 'rebuilding initramfs with the original update-initramfs -u selection'
{
emit_chroot S4
cat <<'REBUILD'
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for command in update-initramfs unmkinitramfs modinfo cmp sha256sum stat; do need_cmd "$command"; done
declare -A before=()
shopt -s nullglob
for image in /boot/initrd.img-*; do
    if [[ -f $image ]]; then before["$image"]=$(stat -Lc '%i:%s:%y:%z' -- "$image"); fi
done
emit INFO "existing initramfs count=${#before[@]}; kernel selection remains update-initramfs -u"
run update-initramfs -u
rewritten=0
for image in /boot/initrd.img-*; do
    [[ -f $image ]] || continue
    signature=$(stat -Lc '%i:%s:%y:%z' -- "$image")
    if [[ $signature != "${before["$image"]:-}" ]]; then
        printf '%s\0' "$image" >&3
        emit OK "rewritten initramfs detected: $image; bytes=$(stat -Lc %s -- "$image")"
        rewritten=$((rewritten+1))
    else
        emit SKIP "initramfs not rewritten: $image"
    fi
done
[[ $rewritten -gt 0 ]] || die 'update-initramfs -u rewrote no initramfs; refusing an unverified success' 95
REBUILD
} | run chroot "$DST_ROOT" /bin/bash -s 3> "$STATE_DIR/rebuilt.paths"
mapfile -d '' -t REBUILT_IMAGES < "$STATE_DIR/rebuilt.paths"
[[ ${#REBUILT_IMAGES[@]} -gt 0 ]] || die 'no rebuilt initramfs paths were returned' 95
emit OK "initramfs rebuild completed; rewritten count=${#REBUILT_IMAGES[@]}"

step S5 'strict package cleanup, cache cleanup and template identities'
emit_chroot S5 > "$STATE_DIR/cleanup.sh"
cat >> "$STATE_DIR/cleanup.sh" <<'CLEAN'
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive
run update-ca-certificates
emit OK 'CA update completed'
run apt-get -qq update
emit OK 'APT index update completed'
run apt-get autoremove --purge -y
emit OK 'package autoremove completed'
rc_list=$(mktemp /tmp/br-initrd-rc.XXXXXX)
run dpkg -l > "$rc_list"
awk '/^rc/ {print $2}' "$rc_list" > "$rc_list.names"
rc_count=$(awk 'END {print NR+0}' "$rc_list.names")
if [[ $rc_count == 0 ]]; then emit SKIP 'no residual-config packages to purge'; else
    emit ACT "purge residual-config packages: count=$rc_count"
    while IFS= read -r package; do emit INFO "residual-config package=$package"; done < "$rc_list.names"
    run xargs -r dpkg -P < "$rc_list.names"
    emit OK "residual-config purge completed: count=$rc_count"
fi
rm -f -- "$rc_list" "$rc_list.names"
run apt-get clean
emit OK 'APT package cache cleanup completed'
remove_paths 'SSH host keys' /etc/ssh/ssh_host_*
: > /etc/machine-id
check 'machine-id exists and is empty' test -f /etc/machine-id
check 'machine-id is zero length' test ! -s /etc/machine-id
remove_paths 'D-Bus identity and random seed' /var/lib/dbus/machine-id /var/lib/systemd/random-seed
clean_tree /var/lib/dhcp contents
clean_tree /var/lib/apt/lists contents
clean_tree /tmp contents
clean_tree /var/tmp contents
clean_tree /var/log logs
remove_paths 'root shell history' /root/.bash_history
clean_tree /home history
export HISTSIZE=0
CLEAN
run chroot "$DST_ROOT" /bin/bash -Eeuo pipefail -s < "$STATE_DIR/cleanup.sh"

# Cleanup can trigger another initramfs rewrite. Verify final files, including
# any additionally changed image, without changing update-initramfs -u selection.
declare -A FINAL_IMAGES=()
for image in "${REBUILT_IMAGES[@]}"; do
    [[ -f $DST_ROOT$image ]] || die "rebuilt initramfs removed by cleanup: $image" 95
    FINAL_IMAGES["$image"]=1
done
for image in "$DST_ROOT"/boot/initrd.img-*; do
    [[ -f $image ]] || continue
    logical=${image#"$DST_ROOT"}
    signature=$(stat -Lc '%i:%s:%y:%z' -- "$image")
    if [[ $signature != "${ORIGINAL_IMAGES["$logical"]:-}" ]]; then FINAL_IMAGES["$logical"]=1; fi
done
mapfile -t REBUILT_IMAGES < <(printf '%s\n' "${!FINAL_IMAGES[@]}" | LC_ALL=C sort)
restore_context

step S6 'read-back acceptance of each rebuilt initramfs and final identities'
{
emit_chroot S6
cat <<'VERIFY'
# Shared by the build hook and verifier: fixed search order, then canonicalize.
# In particular, do not let differing build/chroot PATHs select different ip's.
select_tool_source() {
    for tool_dir in /usr/bin /bin /usr/sbin /sbin; do
        if [ -f "$tool_dir/$1" ] && [ -x "$tool_dir/$1" ]; then
            readlink -f -- "$tool_dir/$1"
            return
        fi
    done
    tool_fallback=$(command -v "$1") || return 1
    [ -f "$tool_fallback" ] && [ -x "$tool_fallback" ] || return 1
    readlink -f -- "$tool_fallback"
}

export PATH=/usr/sbin:/usr/bin:/sbin:/bin
for command in unmkinitramfs modinfo sha256sum cmp realpath readlink stat find; do need_cmd "$command"; done
check 'installed initramfs build hook is executable' test -x /etc/initramfs-tools/hooks/20-iproute2
check 'installed bridge script is executable' test -x /etc/initramfs-tools/scripts/init-premount/10-br-exp
run sh -n /etc/initramfs-tools/scripts/init-premount/10-br-exp
[[ $# -gt 0 ]] || die 'no initramfs was rewritten by update-initramfs -u' 95
work=$(mktemp -d /tmp/br-initrd-verify.XXXXXXXX)
verify_cleanup() {
    local rc=$?
    trap - EXIT
    trap '' HUP INT TERM
    if ! rm -rf -- "$work"; then emit ERR "cannot remove verification workspace: $work" >&2; [[ $rc != 0 ]] || rc=1; fi
    exit "$rc"
}
trap verify_cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
declare -A checked_images=()
verified=0
# Resolve a requested image file without following a link outside this unpack.
# Prefer the usr-merged path; ordinary relative usr-merge symlinks also work.
image_file() {
    local path resolved
    for path in "$@"; do
        [[ -f $main/$path ]] || continue
        resolved=$(realpath -e -- "$main/$path") || return
        [[ $resolved == "$main/"* ]] || continue
        IMAGE_FILE=$resolved
        return 0
    done
    die "required file absent from unpacked initramfs: $*" 95
}
verify_aliases() {
    local tool=$1 private_file=$2 entry
    for entry in "usr/bin/$tool" "usr/sbin/$tool" "bin/$tool" "sbin/$tool"; do
        image_file "$entry"
        check "runtime entry /$entry resolves to private $tool" test "$IMAGE_FILE" = "$private_file"
    done
}
verify_tool() {
    local tool=$1 source=$2 private_file
    image_file "usr/lib/br-exp/$tool"
    private_file=$IMAGE_FILE
    check "initramfs tool $tool is executable: $IMAGE_FILE" test -x "$IMAGE_FILE"
    check "initramfs tool $tool matches target source: $source" cmp -s -- "$source" "$IMAGE_FILE"
    report_file "initramfs tool $tool" "$IMAGE_FILE"
    verify_aliases "$tool" "$private_file"
}
for initrd in "$@"; do
    STAGE=S6-INITRAMFS
    [[ -f $initrd ]] || die "rebuilt initramfs disappeared: $initrd" 95
    checked_images["$initrd"]=1
    kernel=${initrd##*/initrd.img-}
    [[ -n $kernel && $kernel != */* ]] || die "unrecognized initramfs path: $initrd" 95
    emit ACT "read-back verification: image=$initrd kernel=$kernel"
    # Hash the small payload/tools. Do not re-hash the entire compressed image.
    run unmkinitramfs "$initrd" "$work/unpacked"
    if [[ -d $work/unpacked/main ]]; then main=$work/unpacked/main; else main=$work/unpacked; fi
    main=$(realpath -e -- "$main")
    image_file scripts/init-premount/10-br-exp
    check 'embedded bridge script is executable' test -x "$IMAGE_FILE"
    run sh -n "$IMAGE_FILE"
    check 'embedded bridge payload equals the injected MAC-capable version' \
        cmp -s -- /etc/initramfs-tools/scripts/init-premount/10-br-exp "$IMAGE_FILE"
    report_file 'embedded bridge payload' "$IMAGE_FILE"
    ip_source=$(select_tool_source ip)
    sleep_source=$(select_tool_source sleep)
    true_source=$(select_tool_source true)
    emit INFO "canonical tool sources: ip=$ip_source sleep=$sleep_source true=$true_source"
    verify_tool ip "$ip_source"
    verify_tool sleep "$sleep_source"
    verify_tool true "$true_source"
    if udhcpc_source=$(select_tool_source udhcpc); then
        emit ACT "DHCP branch=native; source=$udhcpc_source"
        verify_tool udhcpc "$udhcpc_source"
        emit OK 'AUTO prerequisites: native udhcpc verified'
    elif busybox_source=$(select_tool_source busybox) && "$busybox_source" udhcpc --help >/dev/null 2>&1; then
        emit ACT "DHCP branch=BusyBox wrapper; source=$busybox_source"
        verify_tool br-exp-busybox "$busybox_source"
        image_file usr/lib/br-exp/udhcpc
        wrapper_file=$IMAGE_FILE
        check 'udhcpc wrapper is executable' test -x "$IMAGE_FILE"
        cat > "$work/expected-udhcpc" <<'EXPECTED_UDHCPC'
#!/bin/sh
exec /bin/br-exp-busybox udhcpc "$@"
EXPECTED_UDHCPC
        check 'udhcpc wrapper invokes the verified private BusyBox' cmp -s -- "$work/expected-udhcpc" "$IMAGE_FILE"
        report_file 'udhcpc wrapper' "$IMAGE_FILE"
        verify_aliases udhcpc "$wrapper_file"
        emit OK 'AUTO prerequisites: private BusyBox and udhcpc wrapper verified'
    else
        emit SKIP 'AUTO prerequisites unavailable: target has no usable udhcpc; manual/MAC modes remain supported'
    fi
    for module in bridge llc stp; do
        emit ACT "verify bridge dependency: kernel=$kernel module=$module"
        module_source=$(modinfo -k "$kernel" -n "$module")
        if [[ $module_source == '(builtin)' ]]; then
            emit OK "module=$module kernel=$kernel: built into the kernel; no .ko required in initramfs"
            continue
        fi
        [[ -f $module_source ]] || die "module source missing: $module_source" 95
        module_name=${module_source##*/}
        module_name=${module_name%.zst}; module_name=${module_name%.xz}; module_name=${module_name%.gz}
        find "$main" -type f -path "*/lib/modules/$kernel/*" \
            \( -name "$module_name" -o -name "$module_name.xz" -o -name "$module_name.gz" -o -name "$module_name.zst" \) \
            -print0 > "$work/module.paths"
        mapfile -d '' -t matches < "$work/module.paths"
        [[ ${#matches[@]} == 1 ]] || die "module=$module kernel=$kernel: expected one packed module, found ${#matches[@]}" 95
        vermagic=$(modinfo -F vermagic "${matches[0]}")
        [[ $vermagic == "$kernel "* || $vermagic == "$kernel" ]] || die "module=$module: vermagic mismatch: $vermagic" 95
        emit OK "module=$module file=${matches[0]#"$main/"} vermagic=$vermagic"
    done
    run rm -rf -- "$work/unpacked"
    verified=$((verified+1))
    emit OK "initramfs accepted: $initrd; payload bytes, tools, DHCP branch and bridge dependencies checked"
done
shopt -s nullglob
for initrd in /boot/initrd.img-*; do
    if [[ -f $initrd && ! -v checked_images["$initrd"] ]]; then
        emit SKIP "outside this update-initramfs -u rebuild: $initrd; not covered by this acceptance"
    fi
done
emit OK "accepted rebuilt initramfs count=$verified; actual network/boot behavior requires a boot test"
run rm -rf -- "$work"
check 'verification workspace removed' absent "$work"
trap - EXIT ERR HUP INT TERM
VERIFY
} | run chroot "$DST_ROOT" /bin/bash -s -- "${REBUILT_IMAGES[@]}"
emit OK 'all initramfs rewritten in S4 passed read-back acceptance'
clean_tree "$DST_ROOT/tmp" contents
clean_tree "$DST_ROOT/var/tmp" contents
run install -D -o 0 -g 0 -m 0644 /dev/null "$DST_ROOT/etc/machine-id"
check 'final machine-id is a regular file' test -f "$DST_ROOT/etc/machine-id"
check 'final machine-id is empty' test ! -s "$DST_ROOT/etc/machine-id"
remove_paths 'final identities/history' "$DST_ROOT/var/lib/dbus/machine-id" "$DST_ROOT/var/lib/systemd/random-seed" "$DST_ROOT/root/.bash_history"
clean_tree "$DST_ROOT/var/lib/dhcp" contents
clean_tree "$DST_ROOT/var/log" logs
clean_tree "$DST_ROOT/home" history

step S7 'releasing owned views; archive publication belongs to the build shell'
release_views || die 'owned chroot views could not be released' 95
rm -rf -- "$STATE_DIR"
STATE_DIR=
if [[ -n $REPORT_PATH ]]; then
    python3 - "$REPORT_PATH" "$DST_ROOT" "${REBUILT_IMAGES[@]}" <<'REPORT'
import hashlib
import json
from pathlib import Path
import sys
report, root, *images = sys.argv[1:]
records = []
for image in images:
    path = Path(root + image)
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    records.append({'path': image, 'kernel': image.removeprefix('/boot/initrd.img-'), 'sha256': digest})
with open(report, 'x') as stream:
    json.dump({'transform': 'br-initrd', 'selection': 'update-initramfs -u',
               'accepted_initramfs': records, 'boot_tested': False}, stream, indent=2)
    stream.write('\n')
REPORT
fi
trap - EXIT ERR HUP INT TERM
emit OK "all owned dev/proc/sys views released; initramfs count=${#REBUILT_IMAGES[@]}"
log "DONE: recipe ready: $DST_ROOT"
exit 0

# shellcheck disable=SC2317
: <<'BR_INITRD_EMBEDDED_PAYLOAD'

#===PAYLOAD:10-br-exp:BEGIN===
#!/bin/sh
# initramfs-tools: init-premount script for early bridge (br-exp) setup
# S1–S5; MAC override from B; DHCP dependency limited to AUTO mode.

set -u

PREREQ=""
prereqs() { echo "$PREREQ"; }

case "${1:-}" in
  prereqs) prereqs; exit 0 ;;
esac

# ─────────────────────── Helpers ───────────────────────

log() {
    # $1 = stage, $2 = level, $3 = message
    printf '%s\n' "[br-exp][$1][$2]  $3" > /dev/kmsg
}

cmd_exists() {
    command -v "$1" >/dev/null 2>&1
}

read_sys() {
    # $1 = path
    if [ -r "$1" ]; then
        cat "$1" 2>/dev/null
    fi
}

# 解析 ip= client:server:gw:mask:hostname:device:autoconf
parse_ip() {
    # 期待格式：client:server:gw:mask:hostname:dev:autoconf
    # 这里只关心第 3 段（GW）和第 6 段（DEV）
    IP_GW=""
    IP_DEV=""

    rest=$1
    idx=1

    while :; do
        case "$rest" in
            *:*)
                field=${rest%%:*}
                rest=${rest#*:}
                ;;
            *)
                field=$rest
                rest=""
                ;;
        esac

        case "$idx" in
            3) IP_GW=$field ;;
            6) IP_DEV=$field ;;
        esac

        [ -z "$rest" ] && break
        idx=$((idx + 1))
        [ "$idx" -gt 7 ] && break
    done
}

# ─────────────────────── S0 · Parse cmdline ───────────────────────

# 读取 /proc/cmdline（必要时回退为空）
CMDLINE="$(cat /proc/cmdline 2>/dev/null || echo "")"

# 初始化解析结果变量
BR_EXP_FLAG=0
BR_DEBUG_SHELL=0
IP_RAW=""
IP_GW=""
IP_DEV=""
BR_IF=""
BR_IF_MAC=""
BR_IF_MAC_SET=0
BRIDGE_NAME=""

# 一次性解析所有内核命令行参数
for w in $CMDLINE; do
    case "$w" in
        br_exp=1|br_exp=y|br_exp=Y)
            BR_EXP_FLAG=1
            ;;
        br_exp_debug_shell=1)
            BR_DEBUG_SHELL=1
            ;;
        ip=*)
            # 只取第一个 ip=
            [ -z "$IP_RAW" ] && IP_RAW="${w#ip=}"
            ;;
        br_if=*)
            BR_IF="${w#br_if=}"
            ;;
        br_if_mac=*)
            BR_IF_MAC="${w#br_if_mac=}"
            BR_IF_MAC_SET=1
            ;;
    esac
done

# 没有 br_exp=1 → 完全不启用桥模式，立即退出
if [ "$BR_EXP_FLAG" -ne 1 ]; then
    exit 0
fi

log "S1" "INFO" "br_exp=1 detected, attempting to enable bridge mode"
log "S1" "INFO" "/proc/cmdline = '$CMDLINE'"

# debug shell 模式（人工调试）
if [ "$BR_DEBUG_SHELL" -eq 1 ]; then
    log "S1" "INFO" "br_exp_debug_shell=1 detected, dropping to initramfs shell"
    exec sh
fi

# 必须有 ip=
if [ -z "$IP_RAW" ]; then
    log "S1" "ERR" "ip= parameter not found, cannot proceed with bridge mode"
    exit 0
fi

log "S1" "INFO" "ip= found, raw value: '$IP_RAW'"

# 解析 ip=（只取第 3 段 GW + 第 6 段设备）
parse_ip "$IP_RAW"

# S1-3：ip= 存在但没有第 6 段（设备字段空） → 属于“配置不可靠”，必须立即退出
if [ -z "${IP_DEV-}" ]; then
    log "S1" "WARN" "ip= present but no usable device field (6th segment) could be parsed"
    log "S1" "INFO" "cannot safely infer L3 device; treating this boot as native kernel IP mode"
    log "S1" "INFO" "bridge expansion hook exits early; no changes will be made to network topology"
    exit 0
fi

# 若能走到这里：IP_DEV 一定合法非空 → 它就是桥名
BRIDGE_NAME="${IP_DEV-}"
log "S1" "INFO" "BRIDGE_NAME='$BRIDGE_NAME' (from ip= device field)"

# 是否强制 LOWER 覆盖
if [ "$BR_IF_MAC_SET" -eq 1 ]; then
    log "S1" "INFO" "br_if_mac='$BR_IF_MAC' provided (MAC LOWER override mode)"
    if [ -n "$BR_IF" ]; then
        log "S1" "INFO" "br_if='$BR_IF' ignored because br_if_mac has higher priority"
    fi
elif [ -n "$BR_IF" ]; then
    log "S1" "INFO" "br_if='$BR_IF' provided (manual LOWER override mode)"
else
    log "S1" "INFO" "no br_if provided, AUTO LOWER selection mode"
fi

# ─────────────────────── S1 · 环境与命令检测 ───────────────────────

if cmd_exists ip; then
    IP_CMD="$(command -v ip)"
else
    log "S1" "ERR" "ip command not found, cannot manage interfaces, exiting bridge mode"
    exit 0
fi
log "S1" "INFO" "Using ip command: $IP_CMD"


log "S1" "INFO" "Attempting modprobe bridge/br_netfilter (ignore failures; modules may be built-in)"
modprobe bridge >/dev/null 2>&1 || true
modprobe br_netfilter >/dev/null 2>&1 || true

TEST_BR="br-exp-test.$$"
if "$IP_CMD" link add name "$TEST_BR" type bridge 2>/dev/null; then
    "$IP_CMD" link del "$TEST_BR" 2>/dev/null || true
    log "S1" "INFO" "Bridge capability pre-check passed (test bridge successfully created and deleted)"
else
    log "S1" "ERR" "Kernel bridge support not available (cannot create test bridge), exiting bridge mode"
    exit 0
fi

log "S1" "INFO" "S1 completed, entering S2 (BRIDGE_NAME conflict check)"

# ─────────────────────── S2 · BRIDGE_NAME 冲突检测 ───────────────────────

if [ -d "/sys/class/net/$BRIDGE_NAME" ]; then
    log "S2" "ERR" "Interface named '$BRIDGE_NAME' already exists, cannot create bridge, exiting bridge mode"
    exit 0
fi

log "S2" "INFO" "No existing interface named '$BRIDGE_NAME', safe to create new bridge"
log "S2" "INFO" "S2 completed, entering S3 (LOWER selection mode)"

# ─────────────────────── S3 · br_if_mac / br_if / AUTO 选择 ───────────────────────

LOWER_IF=""

if [ "$BR_IF_MAC_SET" -eq 1 ]; then
    if ! cmd_exists sleep; then
        log "S3" "ERR" "sleep not found; MAC wait unavailable, exiting without fallback"
        exit 0
    fi
    # 匹配 initramfs 阶段的当前 MAC；提供参数后，任何失败均不回退。
    MAC_BYTE='[0123456789abcdefABCDEF][0123456789abcdefABCDEF]'
    case "$BR_IF_MAC" in
        $MAC_BYTE:$MAC_BYTE:$MAC_BYTE:$MAC_BYTE:$MAC_BYTE:$MAC_BYTE)
            ;;
        *)
            log "S3" "ERR" "Invalid br_if_mac='$BR_IF_MAC'; expected six colon-separated hex bytes, exiting without fallback"
            exit 0
            ;;
    esac

    # 使用 shell 内建 printf 归一化大小写，不新增 tr 依赖。
    MAC_REST="$BR_IF_MAC"
    MAC_NORMAL=""
    while :; do
        MAC_PART="${MAC_REST%%:*}"
        MAC_PART="$(printf '%02x' "0x$MAC_PART")"
        MAC_NORMAL="${MAC_NORMAL:+$MAC_NORMAL:}$MAC_PART"
        case "$MAC_REST" in
            *:*) MAC_REST="${MAC_REST#*:}" ;;
            *) break ;;
        esac
    done
    BR_IF_MAC="$MAC_NORMAL"

    MAC_SCANS=11
    MAC_SCAN=1
    log "S3" "INFO" "MAC selection: target='$BR_IF_MAC', wait budget=10s (up to 11 scans, 10 one-second waits)"

    while [ "$MAC_SCAN" -le "$MAC_SCANS" ]; do
        MAC_MATCH_COUNT=0
        MAC_MATCH_IF=""
        MAC_SEEN=""

        # 每轮重新枚举，发现后来出现或已经改名的接口。
        for path in /sys/class/net/*; do
            [ -d "$path" ] || continue
            IF="${path##*/}"
            IF_MAC="$(read_sys "$path/address")"
            MAC_SEEN="${MAC_SEEN:+$MAC_SEEN; }$IF=${IF_MAC:-unreadable}"
            [ -n "$IF_MAC" ] || continue
            if [ "$IF_MAC" = "$BR_IF_MAC" ]; then
                MAC_MATCH_COUNT=$((MAC_MATCH_COUNT + 1))
                MAC_MATCH_IF="$IF"
            fi
        done

        if [ "$MAC_MATCH_COUNT" -gt 1 ]; then
            log "S3" "ERR" "br_if_mac='$BR_IF_MAC' matched $MAC_MATCH_COUNT interfaces, exiting without fallback"
            log "S3" "INFO" "Interfaces in last scan: ${MAC_SEEN:-none}"
            exit 0
        fi

        if [ "$MAC_MATCH_COUNT" -eq 1 ]; then
            if [ "$MAC_MATCH_IF" = "$BRIDGE_NAME" ]; then
                log "S3" "ERR" "MAC matched interface '$MAC_MATCH_IF' equals BRIDGE_NAME='$BRIDGE_NAME', exiting without fallback"
                exit 0
            fi
            # 选用之前重新确认存在性和 MAC；失效则在原预算内重扫。
            MAC_CONFIRM="$(read_sys "/sys/class/net/$MAC_MATCH_IF/address")"
            if [ -d "/sys/class/net/$MAC_MATCH_IF" ] && [ "$MAC_CONFIRM" = "$BR_IF_MAC" ]; then
                LOWER_IF="$MAC_MATCH_IF"
                log "S3" "INFO" "MAC matched: target='$BR_IF_MAC', LOWER_IF='$LOWER_IF', scan=$MAC_SCAN/$MAC_SCANS"
                break
            fi
            log "S3" "INFO" "MAC match '$MAC_MATCH_IF' changed or disappeared before confirmation; will rescan within remaining budget"
        fi

        [ "$MAC_SCAN" -lt "$MAC_SCANS" ] || break
        log "S3" "INFO" "Target MAC not resolved in scan=$MAC_SCAN/$MAC_SCANS; waiting 1s and rescanning"
        if ! sleep 1; then
            log "S3" "ERR" "sleep failed; cannot continue MAC wait, exiting without fallback"
            exit 0
        fi
        MAC_SCAN=$((MAC_SCAN + 1))
    done

    if [ -z "$LOWER_IF" ]; then
        log "S3" "ERR" "Target MAC '$BR_IF_MAC' could not be resolved after $MAC_SCAN scan(s), wait budget exhausted, exiting without fallback"
        log "S3" "INFO" "Interfaces in last scan: ${MAC_SEEN:-none}"
        exit 0
    fi

    log "S3" "INFO" "S3 completed (MAC), entering S5 (bridge create & enslave)"
elif [ -n "$BR_IF" ]; then
    if [ ! -d "/sys/class/net/$BR_IF" ]; then
        log "S3" "ERR" "br_if='$BR_IF' does not exist, exiting bridge mode"
        exit 0
    fi
    if [ "$BR_IF" = "$BRIDGE_NAME" ]; then
        log "S3" "ERR" "br_if='$BR_IF' equals BRIDGE_NAME='$BRIDGE_NAME', invalid, exiting bridge mode"
        exit 0
    fi
    LOWER_IF="$BR_IF"
    log "S3" "INFO" "Manual LOWER override: LOWER_IF='$LOWER_IF'"
    log "S3" "INFO" "S3 completed (manual), entering S5 (bridge create & enslave)"
else
    log "S3" "INFO" "AUTO mode: will scan candidate interfaces in S4"
    log "S3" "INFO" "S3 completed (AUTO), entering S4 (AUTO LOWER selection)"
fi

# ─────────────────────── S4 · AUTO LOWER 选择（udhcpc gw-test） ───────────────────────

if [ -z "$LOWER_IF" ]; then
if cmd_exists udhcpc; then
    UDHCPC_CMD="$(command -v udhcpc)"
    log "S4" "INFO" "Using udhcpc command: $UDHCPC_CMD"
elif cmd_exists busybox && busybox udhcpc --help >/dev/null 2>&1; then
    UDHCPC_CMD="busybox udhcpc"
    log "S4" "INFO" "Using busybox udhcpc wrapper"
else
    log "S4" "ERR" "udhcpc not found, gw-test (DHCP) unavailable, exiting bridge mode"
    exit 0
fi

    CANDIDATES=""
    for path in /sys/class/net/*; do
        [ -d "$path" ] || continue
        IF="${path##*/}"

        case "$IF" in
            lo) continue ;;
        esac

        if [ "$IF" = "$BRIDGE_NAME" ]; then
            continue
        fi

        if [ -d "/sys/class/net/$IF/wireless" ]; then
            continue
        fi

        IF_TYPE="$(read_sys "/sys/class/net/$IF/type")"
        if [ "$IF_TYPE" = "7" ]; then
            continue
        fi

        if [ -z "$CANDIDATES" ]; then
            CANDIDATES="$IF"
        else
            CANDIDATES="$CANDIDATES $IF"
        fi
    done

    if [ -z "$CANDIDATES" ]; then
        log "S4" "ERR" "No suitable candidate interfaces found for AUTO mode, exiting bridge mode"
        exit 0
    fi

    log "S4" "INFO" "AUTO mode enabled, scanning candidate interfaces (excluding lo/wireless/BRIDGE_NAME)"
    log "S4" "INFO" "Initial candidates: $CANDIDATES"

    S4_ROUNDS=8
    UDHCPC_RETRY=1
    UDHCPC_TIMEOUT=1

    ROUND=1
    while [ "$ROUND" -le "$S4_ROUNDS" ] && [ -z "$LOWER_IF" ]; do
        log "S4" "INFO" "ROUND=${ROUND}/${S4_ROUNDS}, current candidates: $CANDIDATES"

        NEXT_CANDIDATES=""

        for IF in $CANDIDATES; do
            log "S4" "INFO" "[ROUND=$ROUND] IF='$IF': bringing link up"
            "$IP_CMD" link set "$IF" up 2>/dev/null || {
                log "S4" "INFO" "[ROUND=$ROUND] IF='$IF': ip link set up failed, skipping"
                continue
            }

            CARRIER_VAL="$(read_sys "/sys/class/net/$IF/carrier")"
            [ -n "$CARRIER_VAL" ] || CARRIER_VAL="unknown"
            log "S4" "INFO" "[ROUND=$ROUND] IF='$IF': carrier=$CARRIER_VAL (ignored for gw-test)"

            UDH_SCRIPT="/bin/true"
            $UDHCPC_CMD -n -q -T "$UDHCPC_TIMEOUT" -t "$UDHCPC_RETRY" -i "$IF" -s "$UDH_SCRIPT" >/dev/null 2>&1
            RET="$?"

            if [ "$RET" -eq 0 ]; then
                log "S4" "INFO" "[ROUND=$ROUND] IF='$IF': DHCP test succeeded (udhcpc exit=0), selecting as LOWER"
                LOWER_IF="$IF"
                break
            else
                log "S4" "INFO" "[ROUND=$ROUND] IF='$IF': DHCP test failed (udhcpc exit=$RET), trying next candidate"
                NEXT_CANDIDATES="${NEXT_CANDIDATES:+$NEXT_CANDIDATES }$IF"
            fi
        done

        if [ -n "$LOWER_IF" ]; then
            break
        fi

        CANDIDATES="$NEXT_CANDIDATES"
        if [ -z "$CANDIDATES" ]; then
            log "S4" "INFO" "[ROUND=$ROUND] No remaining candidates after this round"
            break
        fi

        ROUND=$((ROUND + 1))
    done

    if [ -z "$LOWER_IF" ]; then
        log "S4" "ERR" "Completed $S4_ROUNDS round(s), no interface passed DHCP gw-test, exiting bridge mode"
        exit 0
    fi

    log "S4" "INFO" "AUTO mode selected LOWER_IF='$LOWER_IF'"
fi

log "S4" "INFO" "S4 completed, entering S5 (bridge create & enslave)"

# ─────────────────────── S5 · 创建桥并 enslave LOWER ───────────────────────

log "S5" "INFO" "Creating bridge '$BRIDGE_NAME'"
if ! "$IP_CMD" link add name "$BRIDGE_NAME" type bridge 2>/dev/null; then
    log "S5" "ERR" "Failed to create bridge '$BRIDGE_NAME', exiting bridge mode"
    exit 0
fi

log "S5" "INFO" "Enslaving LOWER_IF='$LOWER_IF' into '$BRIDGE_NAME'"
if ! "$IP_CMD" link set "$LOWER_IF" master "$BRIDGE_NAME" 2>/dev/null; then
    log "S5" "ERR" "Failed to enslave '$LOWER_IF' into '$BRIDGE_NAME', cleaning up"
    "$IP_CMD" link del "$BRIDGE_NAME" 2>/dev/null || true
    exit 0
fi

"$IP_CMD" addr flush dev "$LOWER_IF" 2>/dev/null || true

log "S5" "INFO" "Bringing up bridge '$BRIDGE_NAME' and LOWER_IF='$LOWER_IF'"
"$IP_CMD" link set "$LOWER_IF" up 2>/dev/null || true
"$IP_CMD" link set "$BRIDGE_NAME" up 2>/dev/null || true

log "S5" "INFO" "Bridge setup completed: BRIDGE_NAME='$BRIDGE_NAME', LOWER_IF='$LOWER_IF'"
log "S5" "INFO" "Control will return to initramfs; kernel IP-config will later assign IP to '$BRIDGE_NAME' via ip="

exit 0

#===PAYLOAD:10-br-exp:END===

BR_INITRD_EMBEDDED_PAYLOAD

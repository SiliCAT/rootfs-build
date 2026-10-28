#!/usr/bin/env bash
set -Eeuo pipefail
set +m

usage() {
    cat <<'EOF'
Usage:
  build-rootfs.sh --recipe FILE --output FILE.tar.zst [OPTIONS]

Options:
  --work-dir DIR          Parent for a unique build directory; default: TMPDIR or /tmp.
  --threads N             Zstandard worker threads, 1..64; default: min(nproc, 4).
  --compression-level N   Zstandard level, 1..19; default: 6.
  --keep-work             Retain the rootfs and build.log after success as well.
  --input-rootfs FILE     Pass an existing rootfs archive as the recipe's second argument.
  -h, --help              Show help without creating resources.

Run as root. Builder dependencies must already be installed.
The recipe receives one absolute empty directory and, if supplied, an absolute input archive.
The recipe runs in a private mount namespace; root and mount-namespace privileges are required.
An optional executable verify.sh beside the recipe receives the same directory.
Output contains etc/, usr/, var/ directly, without a rootfs/ prefix.
Existing archives or checksums are never overwritten. Failure retains diagnostics.
EOF
}

log() { printf '[BUILD] %s\n' "$*"; }
die() { printf '[BUILD][ERR] %s\n' "$1" >&2; exit "${2:-1}"; }
need_cmd() { command -v "$1" >/dev/null 2>&1 || die "required command not found: $1" 90; }

# Preserve legal trailing newlines in filenames; realpath itself adds a newline.
canonical() {
    REPLY=$(realpath "$1" -- "$2" && printf '.') || return "$?"
    REPLY=${REPLY%.}
    REPLY=${REPLY%$'\n'}
}

validate_executable() {
    local file=$1 first_line
    [[ -f $file && -r $file && -x $file ]] || die "not a readable executable: $file" 5
    IFS= read -r first_line < "$file" || die "missing LF-terminated shebang: $file" 5
    [[ $first_line == '#!'/* && $first_line != *$'\r'* ]] ||
        die "expected an absolute interpreter and LF shebang: $file" 5
}

positive_integer() {
    local value=$1 maximum=$2 name=$3
    [[ $value =~ ^[0-9]{1,3}$ ]] || die "$name must be an integer from 1 to $maximum" 2
    REPLY=$((10#$value))
    (( REPLY >= 1 && REPLY <= maximum )) || die "$name must be from 1 to $maximum" 2
}

recipe_input=
rootfs_input=
output_input=
work_parent=${TMPDIR:-/tmp}
threads=
compression_level=6
keep_work=0
while (( $# )); do
    case $1 in
        --recipe|--output|--work-dir|--threads|--compression-level|--input-rootfs)
            (( $# >= 2 )) && [[ -n $2 ]] || die "$1 requires a nonempty value" 2
            case $1 in
                --recipe) recipe_input=$2 ;;
                --input-rootfs) rootfs_input=$2 ;;
                --output) output_input=$2 ;;
                --work-dir) work_parent=$2 ;;
                --threads) threads=$2 ;;
                --compression-level) compression_level=$2 ;;
            esac
            shift 2 ;;
        --keep-work) keep_work=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" 2 ;;
    esac
done
[[ -n $recipe_input && -n $output_input ]] || die '--recipe and --output are required' 2
[[ $output_input == *.tar.zst ]] || die '--output must end in .tar.zst' 2
positive_integer "$compression_level" 19 --compression-level
compression_level=$REPLY
if [[ -n $threads ]]; then
    positive_integer "$threads" 64 --threads
    threads=$REPLY
fi

for command in bash id realpath dirname basename mkdir mktemp chmod rm rmdir ln \
    flock setsid unshare env ps pkill awk sleep tee tar zstd sha256sum nproc; do
    need_cmd "$command"
done
[[ $(id -u) == 0 ]] || die 'run this shell as root' 1
[[ -r /proc/self/mountinfo ]] || die '/proc/self/mountinfo must be readable' 90
if [[ -z $threads ]]; then
    threads=$(nproc)
    (( threads <= 4 )) || threads=4
fi

canonical -e "$recipe_input" || die 'cannot resolve the recipe' 5
recipe=$REPLY
validate_executable "$recipe"
recipe_arguments=()
if [[ -n $rootfs_input ]]; then
    canonical -e "$rootfs_input" || die 'cannot resolve input rootfs archive' 5
    rootfs_input=$REPLY
    [[ -f $rootfs_input && -r $rootfs_input ]] || die 'input rootfs must be a readable regular file' 5
    recipe_arguments+=("$rootfs_input")
fi
canonical -e "${BASH_SOURCE[0]}" || die 'cannot resolve the build shell' 5
script_dir=${REPLY%/*}
common_verify=$script_dir/verify-rootfs.sh
validate_executable "$common_verify"
recipe_verify=${recipe%/*}/verify.sh
if [[ -e $recipe_verify || -L $recipe_verify ]]; then
    validate_executable "$recipe_verify"
else
    recipe_verify=
fi

# Resolve the parent separately so an existing output symlink is not followed.
output_name=$(basename -- "$output_input" && printf '.')
output_name=${output_name%.}
output_name=${output_name%$'\n'}
output_parent=$(dirname -- "$output_input" && printf '.')
output_parent=${output_parent%.}
output_parent=${output_parent%$'\n'}
canonical -m "$output_parent" || die 'cannot resolve the output directory' 5
output_parent=$REPLY
output=${output_parent%/}/$output_name
checksum=$output.sha256
lock=$output.lock
for destination in "$output" "$checksum"; do
    [[ ! -e $destination && ! -L $destination ]] || die "output already exists: $destination" 5
done
[[ ! -L $lock ]] || die "output lock is a symlink: $lock" 5
[[ ! -e $lock || -f $lock ]] || die "output lock is not a regular file: $lock" 5
canonical -e "$work_parent" || die '--work-dir must already exist' 5
work_parent=$REPLY
[[ -d $work_parent && -w $work_parent ]] || die '--work-dir must be a writable directory' 5
mkdir -p -- "$output_parent"
exec {output_lock_fd}>> "$lock"
flock --nonblock "$output_lock_fd" || die "another build owns this output: $output" 75
for destination in "$output" "$checksum"; do
    [[ ! -e $destination && ! -L $destination ]] || die "output already exists: $destination" 5
done

run_dir=
publish_dir=
active_pid=
launching=0
publishing=0
signal_rc=0
stop_signal=TERM
checksum_published=0
archive_published=0

on_signal() {
    if (( signal_rc == 0 )); then
        stop_signal=$1
        signal_rc=$2
    fi
    (( launching || publishing )) && return 0
    exit "$signal_rc"
}

# ps reports only identities and state, never command lines or environment data.
# Ignore zombies: they cannot write and a container init may reap them later.
stage_alive() {
    local report
    report=$(ps -eo pid=,pgid=,sid=,stat=) || return 2
    awk -v leader="$active_pid" '
        ($1==leader || $2==leader || $3==leader) && $4 !~ /^Z/ { alive=1 }
        END { exit !alive }
    ' <<< "$report"
}

stop_stage() {
    [[ -n $active_pid ]] || return 0
    local signal iteration alive_rc
    for signal in "$stop_signal" KILL; do
        # The direct-PID fallback also covers cancellation before setsid starts.
        kill -s "$signal" -- "-$active_pid" 2>/dev/null ||
            kill -s "$signal" -- "$active_pid" 2>/dev/null || true
        pkill --signal "$signal" --session "$active_pid" 2>/dev/null || true
        for (( iteration=0; iteration<50; iteration++ )); do
            if stage_alive; then
                sleep 0.2
            else
                alive_rc=$?
                (( alive_rc == 1 )) || return 1
                wait "$active_pid" 2>/dev/null || true
                active_pid=
                return 0
            fi
        done
    done
    return 1
}

cleanup() {
    local rc=$?
    trap - EXIT
    trap '' HUP INT TERM
    set +e
    if ! stop_stage; then
        printf '[BUILD][WARN] stage may still be writing; all work is retained\n' >&2
        (( rc != 0 )) || rc=95
    fi
    if (( checksum_published && !archive_published )); then
        rm -f -- "$checksum"
    fi
    if (( rc != 0 )); then
        [[ -z $run_dir ]] || printf '[BUILD][FAIL] work retained: %s\n' "$run_dir" >&2
        [[ -z $publish_dir ]] || printf '[BUILD][FAIL] archive staging retained: %s\n' "$publish_dir" >&2
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'on_signal HUP 129' HUP
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

run_dir=$(mktemp -d -- "${work_parent%/}/rootfs-build.XXXXXXXX")
rootfs=$run_dir/rootfs
mkdir -m 0755 -- "$rootfs"
build_log=$run_dir/build.log
publish_dir=$(mktemp -d -- "${output_parent%/}/.rootfs-publish.XXXXXXXX")
staged_archive=$publish_dir/$output_name
staged_checksum=$staged_archive.sha256
log "work: $run_dir"

run_stage() {
    local name=$1 rc alive_rc
    shift
    log "$name"
    launching=1
    # A separate session lets cancellation reach the recipe, compiler and logger
    # together. Both the stage and tee status are preserved through pipefail.
    # Parameters below deliberately expand only in the child shell.
    # shellcheck disable=SC2016
    setsid --wait -- env --default-signal=HUP,INT,QUIT,TERM -- \
        bash -c 'set -o pipefail; log=$1; shift; "$@" 2>&1 | tee -a "$log"' \
        rootfs-stage "$build_log" "$@" &
    active_pid=$!
    launching=0
    (( signal_rc == 0 )) || exit "$signal_rc"
    if wait "$active_pid"; then rc=0; else rc=$?; fi
    if stage_alive; then
        printf '[BUILD][ERR] stage left live processes: %s\n' "$name" >&2
        (( rc != 0 )) || rc=95
        exit "$rc"
    else
        alive_rc=$?
        (( alive_rc == 1 )) || die 'cannot establish that the stage stopped' 95
    fi
    active_pid=
    (( rc == 0 )) || exit "$rc"
}

# Isolate only mounts, not users: real UID/GID ownership (including large IDs)
# stays intact. Private propagation prevents recipe mounts from reaching the
# builder. Once all recipe processes exit, even nested sysfs/EFI mounts disappear
# with their namespace; the parent still verifies the actual rootfs before tar.
run_stage 'recipe (private mount namespace)' \
    unshare --mount --propagation private -- "$recipe" "$rootfs" "${recipe_arguments[@]}"
run_stage 'common rootfs verification' "$common_verify" "$rootfs"
if [[ -n $recipe_verify ]]; then
    run_stage 'recipe verification' "$recipe_verify" "$rootfs"
    run_stage 'post-verification mount check' "$common_verify" "$rootfs"
fi

# Parameters below belong to the archive subprocess, not this shell.
# shellcheck disable=SC2016
run_stage 'create tar.zst' bash -c '
    set -o pipefail
    env -u TAR_OPTIONS -u POSIXLY_CORRECT tar \
        --create --file=- --format=pax --numeric-owner --sort=name \
        --pax-option=delete=atime,delete=ctime \
        --acls --xattrs --xattrs-include="*" --selinux --sparse --one-file-system \
        --directory="$1" . |
        env -u ZSTD_CLEVEL -u ZSTD_NBTHREADS zstd --check -T"$3" -"$4" -o "$2"
' rootfs-archive "$rootfs" "$staged_archive" "$threads" "$compression_level"
run_stage 'verify Zstandard integrity' zstd --test -- "$staged_archive"
run_stage 'compare archive with rootfs' env -u TAR_OPTIONS -u POSIXLY_CORRECT tar \
    --zstd --compare --file="$staged_archive" --numeric-owner \
    --acls --xattrs --xattrs-include='*' --selinux --directory="$rootfs"
run_stage 'final rootfs and mount verification' "$common_verify" "$rootfs"

(
    cd -- "$publish_dir"
    sha256sum -- "$output_name" > "$output_name.sha256"
    sha256sum --check -- "$output_name.sha256"
)
chmod 0644 -- "$staged_archive" "$staged_checksum"

# Staging is on the output filesystem. Hard links publish complete files without
# replacing an existing destination; the archive is the final completion marker.
publishing=1
ln -- "$staged_checksum" "$checksum"
checksum_published=1
ln -- "$staged_archive" "$output"
archive_published=1
publishing=0
(( signal_rc == 0 )) || exit "$signal_rc"
rm -- "$staged_archive" "$staged_checksum"
rmdir -- "$publish_dir"
publish_dir=
log "archive ready: $output"
log "checksum: $checksum"

# Publication failures must retain the source and its diagnostics too. Once the
# verified archive is published, failure to remove temporary work is a warning.
if (( keep_work )); then
    log "successful work retained: $run_dir"
elif rm -rf --one-file-system -- "$run_dir"; then
    run_dir=
else
    printf '[BUILD][WARN] archive is ready; temporary work could not be fully removed: %s\n' "$run_dir" >&2
fi
# Keep the lock inode: unlinking it would let concurrent callers lock different files.

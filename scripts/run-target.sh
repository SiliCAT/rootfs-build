#!/usr/bin/env bash
set -euo pipefail

[[ $# == 2 ]] || { printf 'Usage: run-target.sh TARGET_ID STATE_DIRECTORY\n' >&2; exit 2; }
target_id=$1
state=$(realpath -e -- "$2")
[[ -d $state && $(id -u) == 0 ]] || { printf 'Expected root and an existing state directory\n' >&2; exit 1; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository=${script_dir%/*}
mkdir -p -- "$state/work" "$state/output" "$state/logs"
python3 "$script_dir/target.py" resolve "$target_id" --output "$state/target.json"
recipe=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["recipe"])' "$state/target.json")
artifact=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifact_name"])' "$state/target.json")
[[ $(dpkg --print-architecture) == amd64 ]] || { printf 'Native amd64 builder required\n' >&2; exit 1; }

build_pid=
stop() {
    local signal=$1 code=$2
    trap '' HUP INT TERM
    if [[ -n $build_pid ]]; then
        kill -s "$signal" "$build_pid" 2>/dev/null || true
        wait "$build_pid" || true
    fi
    exit "$code"
}
trap 'stop HUP 129' HUP
trap 'stop INT 130' INT
trap 'stop TERM 143' TERM
"$script_dir/build-rootfs.sh" --recipe "$repository/$recipe" \
    --output "$state/output/$artifact.tar.zst" --work-dir "$state/work" \
    --threads 2 --keep-work &
build_pid=$!
if wait "$build_pid"; then rc=0; else rc=$?; fi
build_pid=
(( rc == 0 )) || exit "$rc"

shopt -s nullglob
roots=("$state/work"/rootfs-build.*/rootfs)
(( ${#roots[@]} == 1 )) || { printf 'Expected exactly one completed rootfs\n' >&2; exit 95; }
python3 "$script_dir/target.py" export "$target_id" "${roots[0]}" "$state/output"
(
    cd -- "$state/output"
    sha256sum --check -- "$artifact.SHA256SUMS"
)
# Keep rootfs and build logs until the workflow has collected diagnostics. The
# runner destroys its isolated temporary directory at the end of the job.

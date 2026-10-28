#!/usr/bin/env bash
set -euo pipefail

[[ $# == 1 ]] || { printf 'Usage: run-transform.sh STATE_DIRECTORY\n' >&2; exit 2; }
state=$(realpath -e -- "$1")
[[ -d $state && $(id -u) == 0 ]] || { printf 'Expected root and an existing state directory\n' >&2; exit 1; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repository=${script_dir%/*}
mkdir -p -- "$state/work" "$state/output" "$state/logs"
python3 "$script_dir/transform.py" verify --directory "$state/input" --output "$state/verified-input.json"
readarray -t input_fields < <(python3 - "$state/verified-input.json" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1]))
base = value['source']['base_target']
print(base['architecture'])
print(value['archive'])
print(base['artifact_name'] + '-br-initrd')
PY
)
[[ ${#input_fields[@]} == 3 ]] || { printf 'Invalid input descriptor\n' >&2; exit 95; }
[[ $(dpkg --print-architecture) == "${input_fields[0]}" ]] || {
    printf 'Input architecture must match native builder\n' >&2; exit 95;
}
export BR_INITRD_REPORT=$state/br-initrd.report.json
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
"$script_dir/build-rootfs.sh" --recipe "$repository/transforms/trixie/br-initrd/recipe.sh" \
    --input-rootfs "$state/input/${input_fields[1]}" \
    --output "$state/output/${input_fields[2]}.tar.zst" --work-dir "$state/work" \
    --threads 2 --keep-work &
build_pid=$!
if wait "$build_pid"; then rc=0; else rc=$?; fi
build_pid=
(( rc == 0 )) || exit "$rc"
shopt -s nullglob
roots=("$state/work"/rootfs-build.*/rootfs)
(( ${#roots[@]} == 1 )) || { printf 'Expected exactly one completed rootfs\n' >&2; exit 95; }
python3 "$script_dir/transform.py" export --directory "$state/input" --root "${roots[0]}" \
    --output "$state/output" --report "$BR_INITRD_REPORT"
(
    cd -- "$state/output"
    sha256sum --check -- "${input_fields[2]}.SHA256SUMS"
)

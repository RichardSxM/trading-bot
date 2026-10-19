#!/usr/bin/env bash
# Push lab bundle files to the VPS and import them there (spec 0015 AC-7).
#
#   scripts/push-bundles.sh lab-reports/<batch> [lab-reports/<batch> ...]
#
# Streams every *.bundle.json in the given batch folders as one tar over `tailscale ssh kfs@<host>`
# (KFS_HOST, default kfs-vps) into `sudo -n kfs-bundles`, which unpacks them into
# /var/lib/kfs/bundles/<batch>/ and runs `kfs-compose run --rm gate import`. Its summary prints
# here. Importing a file twice adds nothing, so pushing a folder again is safe.
#
# Exit codes: 0 every file loaded or already there, 1 the transfer failed or a file was refused,
# 64 bad arguments or no bundle file found.
set -euo pipefail

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

[ $# -ge 1 ] || usage
KFS_HOST=${KFS_HOST:-kfs-vps}
command -v tailscale >/dev/null || { echo "push-bundles: tailscale CLI not found" >&2; exit 1; }

files=()
for dir in "$@"; do
    [ -d "$dir" ] || { echo "push-bundles: $dir is not a folder" >&2; exit 64; }
    parent=$(cd "$dir/.." && pwd)
    batch=$(basename "$dir")
    while IFS= read -r -d '' file; do
        files+=("$parent" "$batch/$(basename "$file")")
    done < <(find "$dir" -maxdepth 1 -type f -name '*.bundle.json' -print0)
done
[ ${#files[@]} -gt 0 ] || { echo "push-bundles: no *.bundle.json in $*" >&2; exit 64; }

# tar -C <parent> <batch>/<file> for every file, so each lands in its own batch folder.
args=()
for ((i = 0; i < ${#files[@]}; i += 2)); do
    args+=(-C "${files[i]}" "${files[i + 1]}")
done
echo "push-bundles: sending $(( ${#files[@]} / 2 )) file(s) to $KFS_HOST"
# COPYFILE_DISABLE: macOS tar would otherwise add ._ resource files.
COPYFILE_DISABLE=1 tar -cf - "${args[@]}" | tailscale ssh "kfs@$KFS_HOST" sudo -n kfs-bundles

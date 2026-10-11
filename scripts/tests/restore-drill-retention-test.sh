#!/usr/bin/env bash
# The drill's retention against the nightly backup's snapshot pattern, on the laptop with Docker:
#
#   scripts/tests/restore-drill-retention-test.sh
#
# In a throwaway restic repository inside the backup image, takes a dump snapshot and a `catalog`
# snapshot a minute later on each of three days, with the sha tag changing on day three (a
# deploy). Runs the drill's own RETENTION flags (read from scripts/restore-drill.sh) as a dry run
# with one daily slot, and checks that the newest dump and every catalog snapshot are kept and the
# two older dumps go. Prints PASS, or FAIL with the reason.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

line=$(grep -m 1 '^RETENTION=(' scripts/restore-drill.sh) || { echo "FAIL: no RETENTION line"; exit 1; }
RESTIC_HOST=kfs-vps
eval "$line"
# One daily slot, no weekly or monthly, so the test needs only three days.
flags=()
skip=0
for f in "${RETENTION[@]}"; do
    if ((skip)); then skip=0; continue; fi
    case $f in
    --keep-daily | --keep-weekly | --keep-monthly) skip=1 ;;
    *) flags+=("$f") ;;
    esac
done
flags+=(--keep-daily 1)

docker compose build --quiet backup
BACKUP_IMAGE=$(docker compose config --images | grep 'backup:' | head -n 1)

result=$(docker run --rm -i -u 0 --entrypoint bash "$BACKUP_IMAGE" -s -- "${flags[@]}" <<'EOF'
set -euo pipefail
export RESTIC_REPOSITORY=/tmp/repo RESTIC_PASSWORD=test
restic init -q
mkdir -p /staging/dump /catalog/data /host
echo dump >/staging/dump/db; echo hour >/catalog/data/h; echo env >/host/kfs.env
snap() { restic backup -q --host kfs-vps --time "$@" >/dev/null; }
for d in 2026-10-08:aaa 2026-10-09:aaa 2026-10-10:bbb; do
    day=${d%%:*} sha=${d##*:}
    snap "$day 02:00:00" --tag kfs --tag env:live --tag "sha:$sha" /staging/dump /host/kfs.env
    snap "$day 02:01:00" --tag kfs --tag catalog --tag env:live --tag "sha:$sha" /catalog/data
done
restic forget "$@" --dry-run --json | jq -r '
    [.[] | (.keep // [])[] | "keep \(.time[:10]) \(if (.tags | index("catalog")) then "catalog" else "dump" end)"]
    + [.[] | (.remove // [])[] | "remove \(.time[:10]) \(if (.tags | index("catalog")) then "catalog" else "dump" end)"]
    | sort | .[]'
EOF
)

expected="keep 2026-10-08 catalog
keep 2026-10-09 catalog
keep 2026-10-10 catalog
keep 2026-10-10 dump
remove 2026-10-08 dump
remove 2026-10-09 dump"

if [ "$result" != "$expected" ]; then
    echo "FAIL: the retention plan differs"
    echo "expected:"
    echo "$expected"
    echo "got:"
    echo "$result"
    exit 1
fi
echo PASS

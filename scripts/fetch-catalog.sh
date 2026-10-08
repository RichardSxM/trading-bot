#!/usr/bin/env bash
# Fetch catalog days from the B2 backup onto the laptop (spec 0007 AC-10).
#
#   scripts/fetch-catalog.sh [--into <dir>] <from-date> <to-date>
#   scripts/fetch-catalog.sh [--into <dir>] --backlog
#
# Restores each UTC day from <from-date> to <to-date> (both inclusive, YYYY-MM-DD): its data/ hours,
# their quarantine/ files, gaps/<day>.parquet, and manifests/<day>.json, into ./catalog (or <dir>).
# Each file comes from the newest snapshot tagged `catalog` that holds it, so a day partly retired by
# the VPS retention still restores whole. Files already there are overwritten (same content).
#
# --backlog restores the raw feather of the backlog instead (AC-9): from every snapshot tagged
# `backlog`, the order_book_deltas, trade_tick, and crypto_perpetual files under live/, newest
# snapshot per path, into <dir>/live/. It needs free space of the backlog's size plus 10 GB first.
#
# restic runs in the backup image, read only (--no-lock, --no-cache), as your user. The repository
# is CATALOG_RESTIC_REPOSITORY (environment, else .env); the password and the read only B2 key are
# secrets/b2/restic_password, secrets/b2/read_key_id, and secrets/b2/read_application_key
# (KFS_FETCH_SECRETS names another folder). A local path repository needs no B2 key (the tests).
#
# Exit codes: 0 every day restored, 1 a day had no hour file or an error, 64 bad arguments.
set -euo pipefail

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

die() {
    echo "fetch-catalog: $*" >&2
    exit 1
}

say() {
    echo "fetch-catalog: $*" >&2
}

INTO=./catalog
BACKLOG=0
DAYS=()
while [ $# -gt 0 ]; do
    case $1 in
        --into)
            [ $# -ge 2 ] || usage
            INTO=$2
            shift 2
            ;;
        --backlog)
            BACKLOG=1
            shift
            ;;
        -h | --help) usage ;;
        -*) usage ;;
        *)
            DAYS+=("$1")
            shift
            ;;
    esac
done

# epoch_day YYYY-MM-DD: the day's start as epoch seconds, or nothing for a bad date.
epoch_day() {
    case $1 in [0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]) ;; *) return 0 ;; esac
    if date -u -j -f %Y-%m-%d "$1" +%s >/dev/null 2>&1; then
        date -u -j -f "%Y-%m-%d %H:%M:%S" "$1 00:00:00" +%s
    else
        date -u -d "$1" +%s 2>/dev/null || true
    fi
}

day_of_epoch() {
    date -u -r "$1" +%Y-%m-%d 2>/dev/null || date -u -d "@$1" +%Y-%m-%d
}

if [ "$BACKLOG" = 1 ]; then
    [ ${#DAYS[@]} -eq 0 ] || usage
else
    [ ${#DAYS[@]} -eq 2 ] || usage
    from_s=$(epoch_day "${DAYS[0]}")
    to_s=$(epoch_day "${DAYS[1]}")
    [ -n "$from_s" ] || die "bad date ${DAYS[0]} (use YYYY-MM-DD)"
    [ -n "$to_s" ] || die "bad date ${DAYS[1]} (use YYYY-MM-DD)"
    [ "$from_s" -le "$to_s" ] || die "${DAYS[0]} is after ${DAYS[1]}"
fi

cd "$(git rev-parse --show-toplevel)"

# The repository: the environment wins, else the last CATALOG_RESTIC_REPOSITORY line of .env
# (read, never sourced).
if [ -z "${CATALOG_RESTIC_REPOSITORY:-}" ] && [ -f .env ]; then
    CATALOG_RESTIC_REPOSITORY=$(sed -n 's/^CATALOG_RESTIC_REPOSITORY=//p' .env | tail -n 1)
fi
[ -n "${CATALOG_RESTIC_REPOSITORY:-}" ] || die "CATALOG_RESTIC_REPOSITORY is not set (add b2:<bucket>:kfs to .env)"

SECRETS=${KFS_FETCH_SECRETS:-secrets/b2}
read_secret() {
    [ -s "$SECRETS/$1" ] || die "$SECRETS/$1 is missing or empty$2"
    cat "$SECRETS/$1"
}
RESTIC_PASSWORD=$(read_secret restic_password "")
REPO_MOUNT=()
case $CATALOG_RESTIC_REPOSITORY in
    b2:*)
        key_hint=" (create a B2 key with listBuckets, listFiles, readFiles on this bucket only)"
        B2_ACCOUNT_ID=$(read_secret read_key_id "$key_hint")
        B2_ACCOUNT_KEY=$(read_secret read_application_key "$key_hint")
        RESTIC_REPOSITORY=$CATALOG_RESTIC_REPOSITORY
        ;;
    /* | ./* | ../*)
        [ -d "$CATALOG_RESTIC_REPOSITORY" ] || die "no repository at $CATALOG_RESTIC_REPOSITORY"
        REPO_MOUNT=(-v "$(cd "$CATALOG_RESTIC_REPOSITORY" && pwd):/repo:ro")
        RESTIC_REPOSITORY=/repo
        B2_ACCOUNT_ID=unused B2_ACCOUNT_KEY=unused
        ;;
    *) die "CATALOG_RESTIC_REPOSITORY must be b2:<bucket>:<path> or a local path" ;;
esac
export RESTIC_PASSWORD RESTIC_REPOSITORY B2_ACCOUNT_ID B2_ACCOUNT_KEY

say "building the backup image (restic, jq)"
docker compose build --quiet backup >&2
IMAGE=$(docker compose config --images | grep 'backup:' | head -n 1)
[ -n "$IMAGE" ] || die "no backup image in the Compose config"

mkdir -p "$INTO"
TARGET=$(cd "$INTO" && pwd)
TMP=$TARGET/.fetch-tmp
rm -rf "$TMP"
trap 'rm -rf "$TMP"' EXIT

RESTIC_HOST=kfs-vps
CATALOG=/data/catalog

# restic in the backup image: read only, no cache, as your user, the target at /out.
restic_run() {
    docker run --rm --user "$(id -u):$(id -g)" -e RESTIC_PASSWORD -e RESTIC_REPOSITORY -e B2_ACCOUNT_ID \
        -e B2_ACCOUNT_KEY ${REPO_MOUNT[@]+"${REPO_MOUNT[@]}"} -v "$TARGET:/out" --entrypoint restic "$IMAGE" \
        --no-cache --no-lock "$@"
}

jq_run() {
    docker run --rm -i --entrypoint jq "$IMAGE" "$@"
}

# epoch: restic's RFC 3339 time (nanoseconds, any UTC offset) to epoch seconds (as restore-drill.sh).
JQ_EPOCH='def epoch: sub("\\.[0-9]+"; "") | capture("^(?<d>.{19})(?<z>.*)$")
    | (.d + "Z" | fromdateiso8601) - (if .z == "Z" or .z == "" then 0
      else .z | capture("^(?<s>[+-])(?<h>[0-9]{2}):(?<m>[0-9]{2})$")
        | (if .s == "-" then -1 else 1 end) * ((.h | tonumber) * 3600 + (.m | tonumber) * 60) end);'

# newest_per_path: reads "<snapshot id> <path> <size>" lines and keeps, per path, the line of the
# newest snapshot by its time in $snapshots.
JQ_NEWEST="$JQ_EPOCH"'
    ($snaps | map({key: .id, value: (.time | epoch)}) | from_entries) as $t
    | split("\n") | map(select(length > 0) | split(" ") | {s: .[0], p: .[1], z: (.[2] | tonumber), t: $t[.[0]]})
    | group_by(.p) | map(max_by(.t)) | .[] | "\(.s) \(.p) \(.z)"'

# restore_lines FILE: restores every "<snapshot> <path> <size>" line of FILE, one restic call per
# snapshot, into $TMP, then moves what came back under $CATALOG into the target.
restore_lines() {
    local snap includes line s p z
    for snap in $(cut -d ' ' -f 1 "$1" | sort -u); do
        includes=()
        while IFS=' ' read -r s p z; do
            [ "$s" = "$snap" ] && includes+=(--include "$p")
        done <"$1"
        say "restoring ${#includes[@]} files from snapshot ${snap:0:8}"
        restic_run restore "$snap" --target /out/.fetch-tmp ${includes[@]+"${includes[@]}"} >&2
    done
    if [ -d "$TMP$CATALOG" ]; then
        cp -R "$TMP$CATALOG/." "$TARGET/"
    fi
    rm -rf "$TMP"
}

human() {
    awk -v b="$1" 'BEGIN { split("B KB MB GB TB", u); i = 1; while (b >= 1000 && i < 5) { b /= 1000; i++ }; printf "%.1f %s", b, u[i] }'
}

if [ "$BACKLOG" = 1 ]; then
    snapshots=$(restic_run snapshots --json --tag backlog --host "$RESTIC_HOST") ||
        die "could not list snapshots (wrong password, key, or repository?)"
    ids=$(printf '%s' "$snapshots" | jq_run -r '.[].id')
    [ -n "$ids" ] || die "no snapshot tagged backlog"
    lines=$TARGET/.fetch-lines
    : >"$lines"
    trap 'rm -rf "$TMP" "$lines"' EXIT
    for id in $ids; do
        restic_run ls --json --recursive "$id" "$CATALOG/live" | jq_run -r --arg c "$CATALOG" --arg s "$id" '
            select(.struct_type == "node" and .type == "file")
            | select(.path | test("^" + $c + "/live/[^/]+/((order_book_deltas|trade_tick)/[^/]+/[^/]+|crypto_perpetual_[0-9]+)\\.feather$"))
            | "\($s) \(.path) \(.size)"' >>"$lines"
    done
    newest=$(jq_run -rRs --argjson snaps "$snapshots" "$JQ_NEWEST" <"$lines")
    [ -n "$newest" ] || die "the backlog snapshots hold no feather of the three types"
    printf '%s\n' "$newest" >"$lines"
    need=$(awk '{ s += $3 } END { printf "%d", s }' "$lines")
    free=$(($(df -Pk "$TARGET" | awk 'NR == 2 { print $4 }') * 1024))
    margin=$((10 * 1000 * 1000 * 1000))
    say "backlog: $(wc -l <"$lines" | tr -d ' ') files, $(human "$need"); free $(human "$free")"
    [ "$free" -ge $((need + margin)) ] || die "not enough space: need $(human $((need + margin))) (the backlog plus 10 GB)"
    restore_lines "$lines"
    rm -f "$lines"
    say "backlog restored into $TARGET/live"
    exit 0
fi

snapshots=$(restic_run snapshots --json --tag catalog --host "$RESTIC_HOST") ||
    die "could not list snapshots (wrong password, key, or repository?)"
[ "$(printf '%s' "$snapshots" | jq_run 'length')" -gt 0 ] || die "no snapshot tagged catalog yet"

lines=$TARGET/.fetch-lines
trap 'rm -rf "$TMP" "$lines"' EXIT
missing=()
day_s=$from_s
while [ "$day_s" -le "$to_s" ]; do
    day=$(day_of_epoch "$day_s")
    day_s=$((day_s + 86400))
    # One find per day: the hour files (data and quarantine) start with the day, gaps and the
    # manifest are named after it.
    found=$(restic_run find --json --tag catalog --host "$RESTIC_HOST" "${day}T*" "${day}.parquet" "${day}.json")
    printf '%s' "$found" | jq_run -r --arg c "$CATALOG" '
        .[]? | .snapshot as $s | .matches[]
        | select(.type == "file")
        | select(.path | test("^" + $c + "/(data|quarantine|gaps|manifests)/"))
        | "\($s) \(.path) \(.size)"' | jq_run -rRs --argjson snaps "$snapshots" "$JQ_NEWEST" >"$lines"
    hours=$(grep -c " $CATALOG/data/" "$lines" || true)
    if [ "$hours" -eq 0 ]; then
        say "$day: no hour file in any catalog snapshot"
        missing+=("$day")
        continue
    fi
    grep -q " $CATALOG/manifests/$day.json " "$lines" || say "$day: no manifest yet (the day is still open on the VPS)"
    bytes=$(awk '{ s += $3 } END { printf "%d", s }' "$lines")
    snaps=$(cut -d ' ' -f 1 "$lines" | sort -u | cut -c1-8 | tr '\n' ' ')
    restore_lines "$lines"
    say "$day: $(wc -l <"$lines" | tr -d ' ') files ($hours data), $(human "$bytes"), snapshots ${snaps% }"
done

if [ ${#missing[@]} -gt 0 ]; then
    die "no hours for ${missing[*]}"
fi
say "done: $TARGET"

#!/usr/bin/env bash
# kfs-backup (spec 0005 AC-19): the backup service's nightly job, health check, and drill helper.
# A run that fully succeeds is checked in the repository and pinged to healthchecks.io (spec 0013 AC-5).
#   loop              the container command: secret checks, then one run every night at BACKUP_AT_UTC
#   once              one run now, inside the running container (`kfs-compose exec backup kfs-backup once`)
#   health            the health check: healthy for BACKUP_STALE_H hours after any container start,
#                     then only while the last success is younger than that, and never while the
#                     restic repository is missing
#   compare MANIFEST  compare the database the PG* variables point at with a manifest (restore drill)
# Secrets never reach a log line or a process argument: fingerprints only, the Postgres password in
# a PGPASSFILE on tmpfs, and the B2 key only in the environment of each restic call.
set -euo pipefail

SECRETS=/run/secrets
STAGING=/staging
RUN_DIR=/run/kfs
DUMP_DIR=$STAGING/dump
RUNNING=$STAGING/running
LAST_SUCCESS=$STAGING/last_success
CATALOG=/data/catalog
# The catalog snapshot's cutoff, read by the compactor's retention (spec 0007 AC-6, AC-8).
CATALOG_STATE=/state/catalog_backup.json
# A fixed host name, so restic retention groups every night together.
RESTIC_HOST=kfs-vps
BACKUP_AT_UTC=${BACKUP_AT_UTC:-02:30}
BACKUP_STALE_H=${BACKUP_STALE_H:-26}

# What the manifest records and the restore drill compares. newest_fill_ns is text, because
# nanoseconds do not fit a JSON double.
COUNTS_SQL="SELECT json_build_object(
    'orders', (SELECT count(*) FROM orders),
    'fills', (SELECT count(*) FROM fills),
    'funding_payments', (SELECT count(*) FROM funding_payments),
    'control_commands', (SELECT count(*) FROM control_commands),
    'newest_fill_ns', (SELECT max(filled_ns)::text FROM fills))"
COMPARED=(orders fills funding_payments control_commands newest_fill_ns)

# log LEVEL MSG [EXTRA]: one JSON line in the app processes' shape; EXTRA is a JSON object.
log() {
    local extra=${3:-}
    [[ -n $extra ]] || extra='{}'
    jq -cn --arg ts "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg level "$1" --arg msg "$2" --argjson extra "$extra" \
        '{ts: $ts, level: $level, process: "backup", logger: "backup", msg: $msg} + $extra'
}

# fields --arg name value ... : a JSON object from jq arguments, for log's EXTRA.
fields() {
    jq -cn "$@" '$ARGS.named'
}

last_error() {
    tail -n 1 "$RUN_DIR/err" 2>/dev/null | cut -c1-300
}

refuse() {
    log CRITICAL "$1" "$(fields --arg code invalid_config)"
    exit 1
}

check_config() {
    [[ $BACKUP_AT_UTC =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || refuse "BACKUP_AT_UTC must be HH:MM (UTC)"
    [[ $BACKUP_STALE_H =~ ^[1-9][0-9]*$ ]] || refuse "BACKUP_STALE_H must be a whole number of hours"
    [[ -n ${RESTIC_REPOSITORY:-} ]] || refuse "RESTIC_REPOSITORY is not set"
    [[ -n ${POSTGRES_HOST:-} && -n ${POSTGRES_DB:-} && -n ${POSTGRES_USER:-} ]] ||
        refuse "POSTGRES_HOST, POSTGRES_DB, and POSTGRES_USER must be set"
}

# Spec 0003 AC-12: one `secret loaded` line with a fingerprint per file, or refuse to start.
check_secrets() {
    local name file reason fp
    for name in postgres_password restic_password b2_key_id b2_application_key; do
        file=$SECRETS/$name
        if [[ ! -e $file ]]; then
            reason=missing
        elif [[ ! -s $file ]]; then
            reason=empty
        elif [[ ! -r $file ]]; then
            reason=unreadable
        else
            reason=
        fi
        if [[ -n $reason ]]; then
            log CRITICAL "secret unavailable, refusing to start" \
                "$(fields --arg code secret_unavailable --arg secret "$name" --arg reason "$reason")"
            exit 1
        fi
        fp=$(printf %s "$(<"$file")" | sha256sum | cut -c1-8)
        log INFO "secret loaded" "$(fields --arg secret "$name" --arg fp "$fp")"
    done
}

setup_runtime() {
    local pw
    pw=$(<"$SECRETS/postgres_password")
    pw=${pw//\\/\\\\}
    pw=${pw//:/\\:}
    (umask 077 && printf '*:*:*:%s:%s\n' "$POSTGRES_USER" "$pw" >"$RUN_DIR/pgpass")
    export PGPASSFILE=$RUN_DIR/pgpass PGHOST=$POSTGRES_HOST PGPORT=${POSTGRES_PORT:-5432}
    export PGDATABASE=$POSTGRES_DB PGUSER=$POSTGRES_USER PGCONNECT_TIMEOUT=10
    export RESTIC_PASSWORD_FILE=$SECRETS/restic_password RESTIC_CACHE_DIR=$STAGING/cache
    # A write to a psql that already exited must fail the step, not kill the script.
    trap '' PIPE
}

restic_b2() {
    B2_ACCOUNT_ID=$(<"$SECRETS/b2_key_id") B2_ACCOUNT_KEY=$(<"$SECRETS/b2_application_key") restic "$@"
}

# The repository is created once by hand (verify.md). Without it the backup stays unhealthy.
check_repo() {
    if restic_b2 cat config >/dev/null 2>"$RUN_DIR/err"; then
        rm -f "$RUN_DIR/repo_missing"
        return 0
    fi
    : >"$RUN_DIR/repo_missing"
    log ERROR restic_repo_missing "$(fields --arg code restic_repo_missing --arg error "$(last_error)")"
    return 1
}

# The newest database snapshot (not the catalog's) must be younger than BACKUP_STALE_H, checked
# in the repository itself rather than trusted from the upload (spec 0013 AC-5).
step_verify() {
    local newest now
    STEP=verify
    restic_b2 snapshots --host "$RESTIC_HOST" --latest 1 --json >"$RUN_DIR/snapshots.json" 2>"$RUN_DIR/err" ||
        return 1
    # restic prints its local time with nanoseconds; the container runs in UTC.
    newest=$(jq -r '[.[] | select((.tags // []) | index("catalog") | not)
        | .time | sub("\\.[0-9]+"; "") | sub("\\+00:00$"; "Z") | fromdateiso8601] | max // empty' \
        "$RUN_DIR/snapshots.json" 2>"$RUN_DIR/err") || return 1
    [[ -n $newest ]] || { echo "no database snapshot for host $RESTIC_HOST" >"$RUN_DIR/err"; return 1; }
    now=$(date -u +%s)
    if ((now - newest >= BACKUP_STALE_H * 3600)); then
        echo "newest database snapshot is $(((now - newest) / 3600)) h old" >"$RUN_DIR/err"
        return 1
    fi
    VERIFIED_AGE_S=$((now - newest))
}

# The healthchecks.io `backup-verified` ping (spec 0013 AC-5); its silence is the alert. The URL is
# a secret, so curl reads it from stdin, never from its arguments, and no error text is logged.
# Without a real URL (the laptop's placeholder) it is skipped.
ping_verified() {
    local url=
    if [[ -r $SECRETS/hc_backup_url ]]; then url=$(<"$SECRETS/hc_backup_url"); fi
    if [[ $url != https://* ]]; then
        log INFO "backup-verified ping skipped" "$(fields --arg reason "no ping URL in $SECRETS/hc_backup_url")"
        return 0
    fi
    if printf 'url = "%s"\n' "$url" | curl -fsS -m 10 --retry 3 -o /dev/null -K - 2>/dev/null; then
        log INFO "backup-verified ping sent"
    else
        log WARNING "backup-verified ping failed" "$(fields --arg code backup_ping_failed)"
    fi
}

step_downsample() {
    STEP=downsample
    DOWNSAMPLED=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c 'SELECT downsample_snapshots()' 2>"$RUN_DIR/err") || return 1
}

# One REPEATABLE READ session exports its snapshot and takes the counts. pg_dump then dumps
# exactly that snapshot while the session stays open, so the manifest describes the dump.
step_dump_and_manifest() {
    local fifo=$RUN_DIR/psql.in snap=$RUN_DIR/snapshot counts=$RUN_DIR/counts ready=$RUN_DIR/ready
    local fd pid i ok=1
    STEP=manifest
    rm -f "$fifo" "$snap" "$counts" "$ready"
    rm -rf "$DUMP_DIR"
    mkdir -p "$DUMP_DIR" || return 1
    mkfifo "$fifo" || return 1
    psql -X -q -A -t -v ON_ERROR_STOP=1 <"$fifo" >/dev/null 2>"$RUN_DIR/psql.err" &
    pid=$!
    exec {fd}>"$fifo"
    printf '%s\n' \
        'BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;' \
        "\\o $snap" 'SELECT pg_export_snapshot();' \
        "\\o $counts" "$COUNTS_SQL;" \
        '\o' "\\! touch $ready" 1>&"$fd" 2>/dev/null || true
    # \o closes each file before the next command runs, so `ready` means both are complete.
    for ((i = 0; i < 600; i++)); do
        [[ -e $ready ]] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    if [[ ! -e $ready ]]; then
        cp "$RUN_DIR/psql.err" "$RUN_DIR/err" 2>/dev/null || true
        ok=0
    else
        STEP=dump
        pg_dump --format=custom --snapshot="$(<"$snap")" --file="$DUMP_DIR/kfs.dump" 2>"$RUN_DIR/err" || ok=0
    fi
    printf 'COMMIT;\n' 1>&"$fd" 2>/dev/null || true
    exec {fd}>&-
    wait "$pid" 2>/dev/null || true
    rm -f "$fifo"
    [[ $ok == 1 ]] || return 1

    STEP=manifest
    MANIFEST=$(jq -c --arg created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg env "${TRADING_ENV:-}" \
        --arg git_commit "${KFS_TAG:-unknown}" \
        '{created_at: $created_at, trading_env: $env, git_commit: $git_commit} + .' "$counts" 2>"$RUN_DIR/err") ||
        return 1
    printf '%s\n' "$MANIFEST" >"$DUMP_DIR/manifest.json" 2>"$RUN_DIR/err" || return 1
}

# The dump, the manifest, and the VPS env file and deploy log. The secrets folder is never
# mounted into this container, so it can never be part of a snapshot. The catalog has its own
# snapshot (step_catalog).
step_upload() {
    local paths=("$DUMP_DIR") p
    STEP=upload
    for p in /host/kfs.env /host/deploys.jsonl; do
        [[ -e $p ]] && paths+=("$p")
    done
    restic_b2 backup --json --quiet --host "$RESTIC_HOST" \
        --tag kfs --tag "env:${TRADING_ENV:-}" --tag "sha:${KFS_TAG:-unknown}" \
        "${paths[@]}" >"$RUN_DIR/restic.json" 2>"$RUN_DIR/err" || return 1
    SUMMARY=$(jq -c 'select(.message_type == "summary") | {snapshot_id, bytes_added: .data_added}' \
        "$RUN_DIR/restic.json" 2>"$RUN_DIR/err" | tail -n 1)
    [[ -n $SUMMARY ]] || { echo "restic printed no summary" >"$RUN_DIR/err"; return 1; }
}

# The Parquet catalog in its own snapshot, tagged `catalog`, which the drill never forgets (spec
# 0007 AC-8). Never the raw feather (`live/`), the compactor's state, or a half written `.tmp`.
# Then the cutoff (when this step began) goes to the state file: every catalog file older than it
# is in B2, so the compactor may delete it. The laptop mounts no catalog, so there it is skipped.
step_catalog() {
    local cutoff paths=() p snapshot
    STEP=catalog
    cutoff=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    for p in data quarantine gaps manifests; do
        [[ -e $CATALOG/$p ]] && paths+=("$CATALOG/$p")
    done
    if ((${#paths[@]} == 0)); then
        log INFO "catalog step skipped" "$(fields --arg reason "no catalog files under $CATALOG")"
        return 0
    fi
    restic_b2 backup --json --quiet --host "$RESTIC_HOST" \
        --tag kfs --tag catalog --tag "env:${TRADING_ENV:-}" --tag "sha:${KFS_TAG:-unknown}" \
        --exclude "$CATALOG/live" --exclude "$CATALOG/.compactor" --exclude '*.tmp' \
        "${paths[@]}" >"$RUN_DIR/restic.json" 2>"$RUN_DIR/err" || return 1
    snapshot=$(jq -r 'select(.message_type == "summary") | .snapshot_id' \
        "$RUN_DIR/restic.json" 2>"$RUN_DIR/err" | tail -n 1)
    [[ -n $snapshot && $snapshot != null ]] || { echo "restic printed no summary" >"$RUN_DIR/err"; return 1; }
    jq -cn --arg snapshot_id "$snapshot" --arg cutoff "$cutoff" '{snapshot_id: $snapshot_id, cutoff: $cutoff}' \
        >"$CATALOG_STATE.tmp" 2>"$RUN_DIR/err" || return 1
    mv -f "$CATALOG_STATE.tmp" "$CATALOG_STATE" 2>"$RUN_DIR/err" || return 1
    log INFO "catalog backup done" "$(fields --arg snapshot_id "$snapshot" --arg cutoff "$cutoff")"
}

# One run: every step in order, no retry. A failed step skips the rest of the main chain until the
# next night. The catalog step runs either way, so a database problem never stops retention.
run_backup() {
    local start=$SECONDS
    if ! (set -o noclobber && : >"$RUNNING") 2>/dev/null; then
        log WARNING "backup already running" "$(fields --arg marker "$RUNNING")"
        return 1
    fi
    # However this process ends, a stale marker would block every deploy (AC-12).
    trap 'rm -f "$RUNNING"' EXIT
    log INFO "backup started"
    local ok=0
    check_repo || true
    if step_downsample && step_dump_and_manifest && step_upload; then
        # LAST_SUCCESS still means the main chain passed; the catalog step does not touch it.
        date -u +%s >"$LAST_SUCCESS"
        # An upload proves the repository exists; a brief B2 error in check_repo must not stick.
        rm -f "$RUN_DIR/repo_missing"
        log INFO "backup done" "$(jq -cn --argjson summary "$SUMMARY" --argjson manifest "$MANIFEST" \
            --argjson downsampled "${DOWNSAMPLED:-0}" --argjson duration_s "$((SECONDS - start))" \
            '$summary + {downsampled: $downsampled, duration_s: $duration_s, manifest: $manifest}')"
    else
        log ERROR backup_failed "$(fields --arg code backup_failed --arg step "$STEP" --arg error "$(last_error)")"
        ok=1
    fi
    if ! step_catalog; then
        log ERROR backup_failed "$(fields --arg code backup_failed --arg step "$STEP" --arg error "$(last_error)")"
        ok=1
    fi
    # Only a whole successful run is verified and pinged, so any failure goes quiet outside.
    if [[ $ok == 0 ]]; then
        if step_verify; then
            log INFO "backup verified" "$(fields --argjson newest_age_s "$VERIFIED_AGE_S")"
            ping_verified
        else
            log ERROR backup_failed "$(fields --arg code backup_failed --arg step "$STEP" --arg error "$(last_error)")"
            ok=1
        fi
    fi
    rm -f "$RUNNING"
    return "$ok"
}

seconds_until_next() {
    local now at_s day_s target
    now=$(date -u +%s)
    at_s=$((10#${BACKUP_AT_UTC%%:*} * 3600 + 10#${BACKUP_AT_UTC##*:} * 60))
    day_s=$((now - now % 86400))
    target=$((day_s + at_s))
    ((target > now)) || target=$((target + 86400))
    echo $((target - now))
}

loop() {
    check_config
    check_secrets
    setup_runtime
    # A marker left by a run that was killed; the deploy would wait on it forever.
    rm -f "$RUNNING"
    : >"$RUN_DIR/started"
    trap 'rm -f "$RUNNING"; exit 0' TERM INT
    check_repo || true
    while true; do
        local wait_s
        wait_s=$(seconds_until_next)
        log INFO "backup scheduled" "$(fields --arg at_utc "$BACKUP_AT_UTC" --argjson in_s "$wait_s")"
        sleep "$wait_s" &
        wait $!
        run_backup || true
    done
}

once() {
    check_config
    setup_runtime
    run_backup
}

age_s() {
    echo $(($(date +%s) - $(stat -c %Y "$1")))
}

health() {
    local limit=$((BACKUP_STALE_H * 3600))
    [[ ! -e $RUN_DIR/repo_missing ]] || return 1
    [[ -e $RUN_DIR/started ]] || return 1
    (($(age_s "$RUN_DIR/started") < limit)) && return 0
    [[ -e $LAST_SUCCESS ]] && (($(age_s "$LAST_SUCCESS") < limit))
}

compare() {
    local manifest=${1:?usage: kfs-backup compare MANIFEST} actual diffs
    actual=$(psql -X -q -A -t -v ON_ERROR_STOP=1 -c "$COUNTS_SQL") || {
        echo "FAIL: could not query the restored database"
        return 1
    }
    diffs=$(jq -rn --argjson m "$(<"$manifest")" --argjson a "$actual" \
        '$ARGS.positional[] as $k | select($m[$k] != $a[$k])
         | "  \($k): manifest \($m[$k] // "none"), restored \($a[$k] // "none")"' \
        --args "${COMPARED[@]}")
    if [[ -z $diffs ]]; then
        echo PASS
        return 0
    fi
    echo FAIL
    printf '%s\n' "$diffs"
    return 1
}

case ${1:-} in
loop) loop ;;
once) once ;;
health) health ;;
compare) compare "${2:-}" ;;
*)
    echo "usage: kfs-backup loop|once|health|compare MANIFEST" >&2
    exit 64
    ;;
esac

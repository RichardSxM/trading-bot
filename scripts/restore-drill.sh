#!/usr/bin/env bash
# The monthly restore drill (spec 0005 AC-21), from the laptop.
#
#   scripts/restore-drill.sh [--self-test] [--local]
#
# Restores the newest snapshot of host kfs-vps into a throwaway postgres:16 container,
# runs pg_restore, and compares the row counts and the newest fill time with that snapshot's
# manifest. Prints PASS or FAIL with the differences, removes the container, and only after a
# PASS runs the retention: restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 24 --prune.
# The VPS key cannot delete, so this is the only place retention runs.
#
# It asks for the restic password and the delete capable B2 key (password manager) without
# echo. They live only in this script's environment and reach containers as `docker run -e NAME`,
# never as a value in an argument and never on disk. The repository is $RESTIC_REPOSITORY, or
# asked for (b2:<bucket>:kfs).
#
# --self-test  deletes one fills row from the restored copy before comparing (control_commands
#              or funding_payments when fills is empty), so the drill must print FAIL naming that
#              table. Skips the prune.
# --local      uses the laptop's local repository in the backup-staging volume (no B2 key).
# Exit codes: 0 PASS, 1 FAIL or an error.
set -euo pipefail

SELF_TEST=0
LOCAL=0
for arg in "$@"; do
    case $arg in
    --self-test) SELF_TEST=1 ;;
    --local) LOCAL=1 ;;
    *)
        sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//' >&2
        exit 64
        ;;
    esac
done

die() {
    echo "restore-drill: $*" >&2
    exit 1
}

say() {
    echo "restore-drill: $*" >&2
}

cd "$(git rev-parse --show-toplevel)"

# ask VAR PROMPT: read a secret without echo into VAR (works from a pipe too).
ask() {
    local value
    [ -t 0 ] && printf '%s: ' "$2" >&2
    IFS= read -rs value || true
    [ -t 0 ] && echo >&2
    [ -n "$value" ] || die "$2 is empty"
    export "$1=$value"
}

RESTIC_HOST=kfs-vps
SUFFIX=$$
NET=kfs-drill-$SUFFIX
VOL=kfs-drill-$SUFFIX
PG=kfs-drill-pg-$SUFFIX
DUMP=/restore/staging/dump

STAGING_MOUNT=()
if [ "$LOCAL" = 1 ]; then
    project=$(docker compose config | sed -n 's/^name: //p' | head -n 1)
    staging=$(docker volume ls -q --filter "label=com.docker.compose.project=$project" \
        --filter label=com.docker.compose.volume=backup-staging)
    [ -n "$staging" ] || die "no backup-staging volume for project $project; start the stack first"
    STAGING_MOUNT=(-v "$staging:/staging")
    export RESTIC_REPOSITORY=/staging/restic-repo
    ask RESTIC_PASSWORD "restic password (secrets/restic_password)"
    export B2_ACCOUNT_ID=unused B2_ACCOUNT_KEY=unused
else
    if [ -z "${RESTIC_REPOSITORY:-}" ]; then
        [ -t 0 ] && printf 'restic repository (b2:<bucket>:kfs): ' >&2
        IFS= read -r RESTIC_REPOSITORY || true
        [ -n "$RESTIC_REPOSITORY" ] || die "no repository given"
        export RESTIC_REPOSITORY
    fi
    ask RESTIC_PASSWORD "restic password"
    ask B2_ACCOUNT_ID "B2 key id (the delete capable key)"
    ask B2_ACCOUNT_KEY "B2 application key (the delete capable key)"
fi

say "building the backup image (restic, pg client, kfs-backup compare)"
docker compose build --quiet backup >&2
IMAGE=$(docker compose config --images | grep 'backup:' | head -n 1)
[ -n "$IMAGE" ] || die "no backup image in the Compose config"

cleanup() {
    docker rm -f "$PG" >/dev/null 2>&1 || true
    docker volume rm "$VOL" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create "$NET" >/dev/null
docker volume create "$VOL" >/dev/null

# restic inside the backup image: no cache, so nothing from the repository stays on disk.
restic_run() {
    docker run --rm -e RESTIC_PASSWORD -e RESTIC_REPOSITORY -e B2_ACCOUNT_ID -e B2_ACCOUNT_KEY \
        ${STAGING_MOUNT[@]+"${STAGING_MOUNT[@]}"} -v "$VOL:/restore" --entrypoint restic "$IMAGE" --no-cache "$@"
}

snapshot=$(restic_run snapshots latest --host "$RESTIC_HOST" --json | docker run --rm -i --entrypoint jq "$IMAGE" -r '.[0].short_id // empty') ||
    die "could not list snapshots (wrong password, key, or repository?)"
[ -n "$snapshot" ] || die "no snapshot for host $RESTIC_HOST yet"
say "restoring snapshot $snapshot"
restic_run restore "$snapshot" --include /staging/dump --target /restore >&2

say "starting a throwaway postgres:16"
docker run -d --name "$PG" --network "$NET" -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=drill \
    -v "$VOL:/restore:ro" postgres:16-bookworm >/dev/null
for _ in $(seq 1 60); do
    docker exec "$PG" psql -h 127.0.0.1 -U postgres -d drill -XAtqc 'SELECT 1' >/dev/null 2>&1 && break
    sleep 1
done
docker exec "$PG" psql -h 127.0.0.1 -U postgres -d drill -XAtqc 'SELECT 1' >/dev/null || die "postgres did not start"

say "pg_restore"
docker exec "$PG" pg_restore -h 127.0.0.1 -U postgres -d drill --no-owner --no-privileges --exit-on-error "$DUMP/kfs.dump"

if [ "$SELF_TEST" = 1 ]; then
    tampered=
    for table in fills control_commands funding_payments; do
        tampered=$(docker exec "$PG" psql -h 127.0.0.1 -U postgres -d drill -XAtqc \
            "DELETE FROM $table WHERE ctid = (SELECT ctid FROM $table LIMIT 1) RETURNING '$table'")
        [ -n "$tampered" ] && break
    done
    [ -n "$tampered" ] || die "self-test: fills, control_commands, and funding_payments are all empty; nothing to delete"
    say "self-test: deleted one $tampered row from the restored copy, so this must FAIL naming $tampered"
fi

set +e
docker run --rm --network "$NET" -v "$VOL:/restore:ro" -e PGHOST="$PG" -e PGUSER=postgres -e PGDATABASE=drill \
    "$IMAGE" kfs-backup compare "$DUMP/manifest.json"
result=$?
set -e
say "snapshot $snapshot"

if [ "$result" != 0 ]; then
    exit 1
fi
if [ "$SELF_TEST" = 1 ]; then
    die "self-test: the comparison passed although a row was deleted"
fi

say "retention: 14 daily, 8 weekly, 24 monthly, then prune"
restic_run forget --host "$RESTIC_HOST" --group-by host --keep-daily 14 --keep-weekly 8 --keep-monthly 24 --prune >&2

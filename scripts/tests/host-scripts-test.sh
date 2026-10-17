#!/usr/bin/env bash
# The VPS host scripts' own rules (spec 0005), on the laptop with Docker:
#
#   scripts/tests/host-scripts-test.sh
#
# Runs deploy/host/kfs-compose, kfs-autoheal, and the lib.sh helpers as root inside the backup
# image (bash, jq, flock), with stub `docker` and `kfs-compose` commands on PATH, so nothing
# touches a real Docker or a real VPS. Covers the Compose files per TRADING_ENV (AC-5), the
# health gate leaving out one off containers (AC-4, PR #36), the deploy's pause and drain rules
# (AC-8, AC-11), and the autoheal cap (AC-15). Prints one line per check, then PASS or FAIL.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

docker compose build --quiet backup
BACKUP_IMAGE=$(docker compose config --images | grep 'backup:' | head -n 1)

# KFS_HOST_DIR points the test at another copy of deploy/host (to see a broken rule fail it).
docker run --rm -i -u 0 -v "$(cd "${KFS_HOST_DIR:-deploy/host}" && pwd):/host:ro" --entrypoint bash "$BACKUP_IMAGE" -s <<'EOF'
set -uo pipefail
fails=0
check() { # check <name> <expected> <actual>
    if [ "$2" = "$3" ]; then
        echo "ok   $1"
    else
        echo "FAIL $1"
        echo "     expected: $2"
        echo "     actual:   $3"
        fails=$((fails + 1))
    fi
}
work=$(mktemp -d)
mkdir -p "$work/bin"
export PATH="$work/bin:$PATH"

# --- kfs-compose: the Compose files per TRADING_ENV (AC-5) ---------------------------------
release=0123456789abcdef0123456789abcdef01234567
root="$work/opt"
mkdir -p "$root/releases/$release"
touch "$root/releases/$release/compose.yaml"
ln -s "$root/releases/$release" "$root/current"
cat >"$work/bin/docker" <<'STUB'
#!/bin/sh
echo "KFS_TAG=$KFS_TAG $*"
STUB
chmod +x "$work/bin/docker"
dir="$root/releases/$release"
base="KFS_TAG=$release compose -p kfs --project-directory $dir --env-file $work/kfs.env -f $dir/compose.yaml -f $dir/compose.vps.yaml"

printf 'TRADING_ENV=demo\n' >"$work/kfs.env"
check "kfs-compose demo: no live file" "$base ps" \
    "$(KFS_ROOT=$root KFS_ENV_FILE=$work/kfs.env /host/kfs-compose ps)"
printf 'TRADING_ENV=demo\n  TRADING_ENV = "live"\n' >"$work/kfs.env"
check "kfs-compose live (quoted, last line wins): live file last" "$base -f $dir/compose.live.yaml ps" \
    "$(KFS_ROOT=$root KFS_ENV_FILE=$work/kfs.env /host/kfs-compose ps)"
printf 'TRADING_ENV=live\nTRADING_ENV=demo\n' >"$work/kfs.env"
check "kfs-compose a later demo line wins over live" "$base ps" \
    "$(KFS_ROOT=$root KFS_ENV_FILE=$work/kfs.env /host/kfs-compose ps)"
printf 'TRADING_ENV=demo\n' >"$work/kfs.env"
other=fedcba9876543210fedcba9876543210fedcba98
mkdir -p "$root/releases/$other"
touch "$root/releases/$other/compose.yaml"
check "kfs-compose KFS_RELEASE picks the folder and the image tag" \
    "KFS_TAG=$other compose -p kfs --project-directory $root/releases/$other --env-file $work/kfs.env -f $root/releases/$other/compose.yaml -f $root/releases/$other/compose.vps.yaml ps" \
    "$(KFS_RELEASE=$other KFS_ROOT=$root KFS_ENV_FILE=$work/kfs.env /host/kfs-compose ps)"
KFS_ROOT=$root KFS_ENV_FILE=$work/missing.env /host/kfs-compose ps >/dev/null 2>&1
check "kfs-compose refuses a missing env file" 1 "$?"
KFS_RELEASE=nosuch KFS_ROOT=$root KFS_ENV_FILE=$work/kfs.env /host/kfs-compose ps >/dev/null 2>&1
check "kfs-compose refuses a release with no bundle" 1 "$?"

# --- lib.sh: kfs_trading_env ------------------------------------------------------------------
export KFS_ROOT=$root KFS_LOG_DIR=$work/log KFS_STATE_DIR=$work/state
mkdir -p "$KFS_STATE_DIR"
# shellcheck source=/dev/null
. /host/lib.sh
KFS_ENV_FILE=$work/kfs.env
printf 'OTHER=1\n' >"$work/kfs.env"
check "kfs_trading_env defaults to demo" demo "$(kfs_trading_env)"
printf "TRADING_ENV='live'\n" >"$work/kfs.env"
check "kfs_trading_env reads a quoted live" live "$(kfs_trading_env)"

# --- lib.sh: kfs_services_healthy, the deploy's health gate (AC-4) ----------------------------
mkdir -p "$root/current/deploy/host"
ps_json="$work/ps.json"
cat >"$root/current/deploy/host/kfs-compose" <<STUB
#!/bin/sh
case "\$*" in
    "ps -a --format json") cat "$ps_json" ;;
    "config --services") printf 'migrate\ntrader\ndashboard\n' ;;
esac
STUB
chmod +x "$root/current/deploy/host/kfs-compose"
row() { # row <service> <state> <health> <exit> [oneoff]
    printf '{"Service":"%s","State":"%s","Health":"%s","ExitCode":%s,"Labels":"com.docker.compose.project=kfs%s"}\n' \
        "$1" "$2" "$3" "$4" "${5:+,com.docker.compose.oneoff=True}"
}
{ row migrate exited "" 0; row trader running healthy 0; row dashboard running healthy 0; } >"$ps_json"
out=$(kfs_services_healthy); rc=$?
check "healthy gate passes when all are healthy and migrate exited 0" "0 " "$rc $out"
# A one off `kfs-compose run dashboard` shares the service name; it must not stand in for it.
{ row migrate exited "" 0; row trader running healthy 0; row dashboard running unhealthy 0; row dashboard running starting 0 oneoff; } >"$ps_json"
out=$(kfs_services_healthy); rc=$?
check "healthy gate ignores a one off and reports the real unhealthy service" "1 dashboard: unhealthy" "$rc $out"
{ row migrate exited "" 0; row trader running healthy 0; row dashboard running healthy 0 oneoff; } >"$ps_json"
out=$(kfs_services_healthy); rc=$?
check "healthy gate: a one off alone does not count as the service" "1 dashboard: no container" "$rc $out"
{ row migrate exited "" 1; row trader running starting 0; row dashboard running healthy 0; } >"$ps_json"
out=$(kfs_services_healthy); rc=$?
check "healthy gate reports a failed migrate and a starting trader" "1 migrate: exited exit 1 trader: starting" "$rc $out"
# Compose v2 before 2.21 prints one JSON array instead of one object per line.
{ row migrate exited "" 0; row trader running healthy 0; row dashboard running healthy 0; } | jq -sc . >"$ps_json"
out=$(kfs_services_healthy); rc=$?
check "healthy gate reads the one array form too" "0 " "$rc $out"

# --- lib.sh: kfs_pause_and_wait, the deploy's drain (AC-8, AC-11) -----------------------------
# kfs_control is replaced by a stub that answers from variables and records what was sent.
calls="$work/calls"
kfs_control() {
    echo "$*" >>"$calls"
    case $1 in
        state) [ "$STATE_RC" = 0 ] && echo "{\"data\":{\"paused\":$PAUSED}}"; return "$STATE_RC" ;;
        pause) return "$PAUSE_RC" ;;
        unpause) return 0 ;;
        wait-flat) return "$FLAT_RC" ;;
    esac
}
drain() { # drain <now> -> "<rc> <paused_by_us> <commands sent>"
    : >"$calls"
    kfs_pause_and_wait "$1" 15m >/dev/null
    local rc=$?
    echo "$rc $KFS_PAUSED_BY_US $(cut -d' ' -f1 "$calls" | paste -sd, -)"
}
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=0
check "drain: pause, wait, flat" "0 true state,pause,wait-flat" "$(drain false)"
STATE_RC=0 PAUSED=true PAUSE_RC=0 FLAT_RC=0
check "drain: a pause already there is left alone and never lifted" "0 false state,wait-flat" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=2
check "drain: not flat in time lifts our pause" "$KFS_DRAIN_NOT_FLAT true state,pause,wait-flat,unpause" "$(drain false)"
STATE_RC=0 PAUSED=true PAUSE_RC=0 FLAT_RC=2
check "drain: not flat with your pause sends no unpause" "$KFS_DRAIN_NOT_FLAT false state,wait-flat" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=3
check "drain: Resume pressed during the wait aborts, nothing more sent" "$KFS_DRAIN_PAUSE_LIFTED true state,pause,wait-flat" "$(drain false)"
STATE_RC=1 PAUSED=false PAUSE_RC=0 FLAT_RC=0
check "drain: no trader_state fails closed, no pause sent" "$KFS_DRAIN_NO_STATE false state" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=1 FLAT_RC=0
check "drain: an unconfirmed pause aborts and is lifted" "$KFS_DRAIN_PAUSE_FAILED true state,pause,unpause" "$(drain false)"
STATE_RC=1 PAUSED=false PAUSE_RC=0 FLAT_RC=0
check "drain --now: no trader_state still pauses, no wait" "0 true state,pause" "$(drain true)"
STATE_RC=0 PAUSED=false PAUSE_RC=1 FLAT_RC=0
check "drain --now: an unconfirmed pause goes on and still counts as ours" "0 true state,pause" "$(drain true)"
: >"$calls"; KFS_PAUSED_BY_US=false; kfs_unpause_if_ours >/dev/null
check "unpause only a pause we sent" "" "$(cat "$calls")"

# --- kfs-autoheal: restart unhealthy containers, give up after 3 an hour (AC-15) --------------
cat >"$work/bin/docker" <<STUB
#!/bin/sh
case "\$1" in
    ps) cat "$work/unhealthy" ;;
    restart) echo "\$2" >>"$work/restarted" ;;
esac
STUB
chmod +x "$work/bin/docker"
heal() { KFS_STATE_DIR=$work/state KFS_LOG_DIR=$work/log /host/kfs-autoheal; }
codes() { jq -r '.msg' | paste -sd, -; }
printf 'kfs-trader-1\n' >"$work/unhealthy"
: >"$work/restarted"
rm -f "$work/state/autoheal.json"
check "autoheal: three restarts in an hour" "autoheal_restart,autoheal_restart,autoheal_restart" \
    "$(for _ in 1 2 3; do heal; done | codes)"
check "autoheal: the fourth gives up once" "autoheal_gave_up" "$(heal | codes)"
check "autoheal: no second give up within the hour, and no restart" "|3" "$(heal | codes)|$(wc -l <"$work/restarted" | tr -d ' ')"
# Restarts older than an hour drop out of the window, the give up note with them.
old=$(($(date -u +%s) - 4000))
jq -c --argjson t "$old" '.restarts["kfs-trader-1"] = [$t, $t, $t] | .gave_up_at["kfs-trader-1"] = $t' \
    "$work/state/autoheal.json" >"$work/s" && mv "$work/s" "$work/state/autoheal.json"
check "autoheal: an hour later it restarts again" "autoheal_restart" "$(heal | codes)"
printf 'kfs-recorder-1\n' >"$work/unhealthy"
check "autoheal: the cap is per container" "autoheal_restart" "$(heal | codes)"
printf 'not json' >"$work/state/autoheal.json"
check "autoheal: an unreadable state file starts afresh" "autoheal state unreadable, starting afresh,autoheal_restart" "$(heal | codes)"
check "autoheal: the state file is 0600" "600" "$(stat -c %a "$work/state/autoheal.json")"
# While a deploy holds its lock, autoheal leaves every container alone.
mkdir -p /run/lock
: >"$work/restarted"
(
    exec 8>/run/lock/kfs-deploy.lock
    flock -x 8
    heal >"$work/held.out"
)
check "autoheal: does nothing while a deploy holds the lock" "|0" "$(cat "$work/held.out")|$(wc -l <"$work/restarted" | tr -d ' ')"
rm -f /run/lock/kfs-deploy.lock

rm -rf "$work"
if [ "$fails" -eq 0 ]; then echo PASS; else echo "FAIL: $fails checks"; exit 1; fi
EOF

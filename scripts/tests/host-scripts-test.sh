#!/usr/bin/env bash
# The VPS host scripts' own rules (spec 0005), on the laptop with Docker:
#
#   scripts/tests/host-scripts-test.sh
#
# Runs deploy/host/kfs-compose, kfs-autoheal, and the lib.sh helpers as root inside the backup
# image (bash, jq, flock), with stub `docker` and `kfs-compose` commands on PATH, so nothing
# touches a real Docker or a real VPS. Covers the Compose files per TRADING_ENV (AC-5), the
# health gate leaving out one off containers (AC-4, PR #36), the deploy's pause and drain rules
# (AC-8, AC-11), the autoheal cap (AC-15), and the limits of kfs-bundles (spec 0015 AC-7).
# Prints one line per check, then PASS or FAIL.
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
# A confirmed pause makes the trader paused by `deploy`; TAKEOVER=1 has you press Pause on the
# dashboard during the wait; STATE_AFTER_RC is how the state read after the wait goes.
kfs_control() {
    echo "$*" >>"$calls"
    case $1 in
        state)
            local rc=$STATE_RC
            [ "$WAITED" = 1 ] && rc=$STATE_AFTER_RC
            [ "$rc" = 0 ] && echo "{\"data\":{\"paused\":$PAUSED,\"pause_source\":\"$SOURCE\"}}"
            return "$rc"
            ;;
        pause)
            [ "$PAUSE_RC" = 0 ] && PAUSED=true SOURCE=deploy
            return "$PAUSE_RC"
            ;;
        unpause) return 0 ;;
        wait-flat)
            WAITED=1
            [ "$TAKEOVER" = 1 ] && PAUSED=true SOURCE=dashboard
            return "$FLAT_RC"
            ;;
    esac
}
drain() { # drain <now> -> "<rc> <paused_by_us> <commands sent>"
    : >"$calls"
    WAITED=0 SOURCE=${SOURCE:-} TAKEOVER=${TAKEOVER:-0} STATE_AFTER_RC=${STATE_AFTER_RC:-0}
    kfs_pause_and_wait "$1" 15m >/dev/null
    local rc=$?
    echo "$rc $KFS_PAUSED_BY_US $(cut -d' ' -f1 "$calls" | paste -sd, -)"
}
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=0 SOURCE= TAKEOVER=0 STATE_AFTER_RC=0
check "drain: pause, wait, flat" "0 true state,pause,wait-flat" "$(drain false)"
STATE_RC=0 PAUSED=true PAUSE_RC=0 FLAT_RC=0
check "drain: a pause already there is left alone and never lifted" "0 false state,wait-flat" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=2
check "drain: not flat in time lifts our pause" "$KFS_DRAIN_NOT_FLAT true state,pause,wait-flat,state,unpause" "$(drain false)"
STATE_RC=0 PAUSED=true PAUSE_RC=0 FLAT_RC=2
check "drain: not flat with your pause sends no unpause" "$KFS_DRAIN_NOT_FLAT false state,wait-flat" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=3
check "drain: Resume pressed during the wait aborts, nothing more sent" "$KFS_DRAIN_PAUSE_LIFTED true state,pause,wait-flat" "$(drain false)"
STATE_RC=1 PAUSED=false PAUSE_RC=0 FLAT_RC=0
check "drain: no trader_state fails closed, no pause sent" "$KFS_DRAIN_NO_STATE false state" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=1 FLAT_RC=0
check "drain: an unconfirmed pause aborts and is lifted (it may still be queued)" "$KFS_DRAIN_PAUSE_FAILED true state,pause,state,unpause" "$(drain false)"
STATE_RC=1 PAUSED=false PAUSE_RC=0 FLAT_RC=0
check "drain --now: no trader_state still pauses, no wait" "0 true state,pause" "$(drain true)"
STATE_RC=0 PAUSED=false PAUSE_RC=1 FLAT_RC=0
check "drain --now: an unconfirmed pause goes on and still counts as ours" "0 true state,pause" "$(drain true)"
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=2 TAKEOVER=1
check "drain: a Pause you press during the wait is never lifted (review 2026-10-17)" "$KFS_DRAIN_NOT_FLAT true state,pause,wait-flat,state" "$(drain false)"
STATE_RC=0 PAUSED=false PAUSE_RC=0 FLAT_RC=2 TAKEOVER=0 STATE_AFTER_RC=1
check "drain: unreadable state after the wait leaves the pause in place" "$KFS_DRAIN_NOT_FLAT true state,pause,wait-flat,state" "$(drain false)"
STATE_AFTER_RC=0
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

# --- kfs-deploy run: an outcome line on every way out, output kept in deploy-run.log ----------
# (review 2026-10-17: a deploy cut off from the laptop must still leave a trace and an alert.)
sha=1111111111111111111111111111111111111111
droot="$work/deploy-root"
mkdir -p "$droot/releases/$sha/deploy" "$work/dlog" "$work/dsecrets"
cp -R /host "$droot/releases/$sha/deploy/host"
touch "$droot/releases/$sha/compose.yaml" "$droot/releases/$sha/compose.vps.yaml"
printf 'x' >"$work/dsecrets/ghcr_token"
printf 'TRADING_ENV=demo\n' >"$work/denv"
cat >"$work/bin/df" <<'STUB'
#!/bin/sh
printf 'Avail\n999999999999\n'
STUB
cat >"$work/bin/docker" <<STUB
#!/bin/sh
case "\$*" in
    *"config --images"*) echo "ghcr.io/x/kfs-trader:$sha" ;;
    "manifest inspect"*) [ -e "$work/hang" ] && exec sleep 60; exit 1 ;;
esac
STUB
chmod +x "$work/bin/df" "$work/bin/docker"
deploy() {
    KFS_ROOT=$droot KFS_LOG_DIR=$work/dlog KFS_SECRETS_DIR=$work/dsecrets KFS_ENV_FILE=$work/denv \
        "$droot/releases/$sha/deploy/host/kfs-deploy" run "$sha" >/dev/null 2>&1
}
deploy; rc=$?
check "kfs-deploy: a refused preflight exits 4 with one outcome line" "4 preflight_refused" \
    "$rc $(jq -r .outcome "$work/dlog/deploys.jsonl" | paste -sd, -)"
check "kfs-deploy: its output is kept in deploy-run.log" "1" "$(grep -c 'preflight refused' "$work/dlog/deploy-run.log")"
touch "$work/hang"
# Started directly (not through the function), so $! is the deploy's own bash.
KFS_ROOT=$droot KFS_LOG_DIR=$work/dlog KFS_SECRETS_DIR=$work/dsecrets KFS_ENV_FILE=$work/denv \
    "$droot/releases/$sha/deploy/host/kfs-deploy" run "$sha" >/dev/null 2>&1 &
pid=$!
for _ in $(seq 1 50); do pgrep -f "sleep 60" >/dev/null && break; sleep 0.1; done
# bash acts on the TERM once its foreground command returns, so end the hung one too.
kill -TERM "$pid"; kill $(pgrep -f "sleep 60") 2>/dev/null || true
wait "$pid"; rc=$?
check "kfs-deploy: a deploy killed halfway records interrupted" "143 preflight_refused,interrupted" \
    "$rc $(jq -r .outcome "$work/dlog/deploys.jsonl" | paste -sd, -)"
check "kfs-deploy: and logs a CRITICAL line saying so" "CRITICAL" \
    "$(grep 'deploy ended without an outcome' "$work/dlog/host.jsonl" | jq -r .level)"

# --- kfs-deploy run: the promotion check before the drain (spec 0015 AC-26) --------------------
# A running release (so the check applies), images that exist, and a stub trader and gate: the
# gate's exit code comes from $work/gate_rc, and it prints a log line before its result, as the
# real one does on stdout.
prev=2222222222222222222222222222222222222222
mkdir -p "$droot/releases/$prev/deploy" "$work/plog"
cp -R /host "$droot/releases/$prev/deploy/host"
touch "$droot/releases/$prev/compose.yaml" "$droot/releases/$prev/compose.vps.yaml"
ln -sfn "releases/$prev" "$droot/current"
rm -f "$work/hang"
cat >"$work/bin/docker" <<STUB
#!/bin/sh
case "\$*" in
    *"config --images"*) echo "ghcr.io/x/kfs-trader:$sha" ;;
    "manifest inspect"*) exit 0 ;;
    *"exec -T backup"*) exit 1 ;;
    *"pull --quiet"*) exit 0 ;;
    *"run --rm --no-deps -T migrate"*)
        echo migrate >>"$work/calls"
        exit "\$(cat "$work/migrate_rc")" ;;
    *"run --rm --no-deps -T trader python -m trader.version"*)
        printf '{\n  "frozen_hash": "f1",\n  "git_commit": "%s"\n}\n' "$sha" ;;
    *"run --rm --no-deps -T gate check-version"*)
        echo check >>"$work/calls"
        cat >"$work/gate_stdin"
        rc=\$(cat "$work/gate_rc")
        echo '{"msg": "gate_check_version", "stage_needed": "paper", "version_id": "log-line"}'
        [ "\$rc" -le 3 ] && printf '{"frozen_hash": "f1", "stage_needed": "paper", "verdict": "%s", "version_id": "v-1"}\n' \
            "\$( [ "\$rc" -eq 0 ] && echo pass || echo missing)"
        exit "\$rc" ;;
    *) exit 1 ;;
esac
STUB
chmod +x "$work/bin/docker"
pdeploy() {
    KFS_ROOT=$droot KFS_LOG_DIR=$work/plog KFS_SECRETS_DIR=$work/dsecrets KFS_ENV_FILE=$work/denv \
        "$droot/releases/$sha/deploy/host/kfs-deploy" run "$sha" >"$work/pout" 2>&1
}
echo 0 >"$work/migrate_rc"
echo 3 >"$work/gate_rc"
pdeploy; rc=$?
check "kfs-deploy: a version without its paper pass is refused before the drain" "4 preflight_refused" \
    "$rc $(jq -r .outcome "$work/plog/deploys.jsonl" | paste -sd, -)"
check "kfs-deploy: the refusal names the version and verdict" "promotion check: version v-1 has no candidate to paper pass (verdict missing)" \
    "$(grep 'preflight refused' "$work/plog/host.jsonl" | jq -r .reason)"
check "kfs-deploy: and prints the promote command for it" "1" \
    "$(grep -c 'gate promote --manual --step candidate-paper --version v-1 ' "$work/pout")"
check "kfs-deploy: the gate reads the new trader's version on stdin" "f1 $sha" \
    "$(jq -r '"\(.frozen_hash) \(.git_commit)"' "$work/gate_stdin")"
check "kfs-deploy: nothing is paused" "0" "$(grep -c 'pause' "$work/plog/host.jsonl")"
echo 1 >"$work/gate_rc"
: >"$work/plog/deploys.jsonl"
pdeploy; rc=$?
check "kfs-deploy: a gate error refuses too" "4 promotion check: gate check-version failed (exit 1)" \
    "$rc $(grep 'preflight refused' "$work/plog/host.jsonl" | tail -n 1 | jq -r .reason)"
echo 0 >"$work/gate_rc"
: >"$work/plog/deploys.jsonl"
: >"$work/calls"
pdeploy; rc=$?
check "kfs-deploy: a version with its pass goes on to the drain" "v-1" \
    "$(grep 'promotion check passed' "$work/plog/host.jsonl" | jq -r .version_id)"
check "kfs-deploy: past the check, not refused" "no" \
    "$(jq -r .outcome "$work/plog/deploys.jsonl" | grep -qx preflight_refused && echo refused || echo no)"
check "kfs-deploy: the new migrate runs before the check reads the schema" "migrate,check" \
    "$(paste -sd, "$work/calls")"
echo 1 >"$work/migrate_rc"
: >"$work/plog/deploys.jsonl"
: >"$work/calls"
pdeploy; rc=$?
check "kfs-deploy: a failed migrate refuses before the check" "4 migrate failed in the new image (exit 1) migrate" \
    "$rc $(grep 'preflight refused' "$work/plog/host.jsonl" | tail -n 1 | jq -r .reason) $(paste -sd, "$work/calls")"

# --- kfs-bundles: the receive script's limits (spec 0015 AC-7, review 2026-10-22) -----------
# The VPS has GNU tar and a real df: drop the df stub above, and add GNU tar to this Alpine image.
rm -f "$work/bin/df"
apk add --quiet --no-cache tar >/dev/null
bstate="$work/bstate"
bsrc="$work/bsrc/batch"
mkdir -p "$bsrc"
cat >"$work/bin/bundles-compose" <<'STUB'
#!/bin/sh
echo "compose $*"
STUB
chmod +x "$work/bin/bundles-compose"
bundles() { # bundles [max bytes] <tar on stdin>
    KFS_STATE_DIR=$bstate KFS_COMPOSE=bundles-compose KFS_BUNDLES_MAX_BYTES=${1:-1048576} /host/kfs-bundles 2>&1
}
echo '{}' >"$bsrc/a.bundle.json"
echo 'x' >"$bsrc/notes.txt"
out=$(tar -c -f - -C "$work/bsrc" batch | bundles); rc=$?
check "kfs-bundles: a good archive installs its bundle files and imports" "0 compose run --rm gate import" \
    "$rc $(echo "$out" | tail -n 1)"
check "kfs-bundles: only the bundle files are kept" "batch/a.bundle.json" \
    "$(cd "$bstate/bundles" && find . -type f | sed 's|^\./||')"
out=$(tar -c -f - -C "$work/bsrc" batch | bundles 100); rc=$?
check "kfs-bundles: an archive over the cap is refused" "1 kfs-bundles: refused: the archive is over 100 bytes" \
    "$rc $out"
ln -s /etc/passwd "$bsrc/b.bundle.json"
out=$(tar -c -f - -C "$work/bsrc" batch | bundles); rc=$?
check "kfs-bundles: a link named like a bundle is refused whole" \
    "1 kfs-bundles: refused: 1 member(s) named *.bundle.json are not regular files" "$rc $out"
check "kfs-bundles: and nothing of it is installed" "batch/a.bundle.json" \
    "$(cd "$bstate/bundles" && find . ! -type d | sed 's|^\./||')"

rm -rf "$work"
if [ "$fails" -eq 0 ]; then echo PASS; else echo "FAIL: $fails checks"; exit 1; fi
EOF

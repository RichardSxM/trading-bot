#!/usr/bin/env bash
# The restart and outage drills (spec 0023 AC-24, the Drills table).
#
#   scripts/restart-drill.sh <demo|live> [D1 D8 ...]
#
# demo runs on the laptop against the running Compose stack (TRADING_ENV=demo): D1, D8, D9, D10, D11.
# live runs on the VPS as root (sudo, through kfs-compose) with TRADING_ENV=live and LIVE_MODE=drill:
# all twelve. Name drills to run only those. For each drill it prints the expected outcome, the
# evidence it collected (the trader's log lines, trader_state, Postgres rows in demo, Kraken's view
# in live), and PASS or FAIL. Every drill must end flat, with no open order and no halt left behind.
#
# How it acts on the trader:
# - kill: SIGKILL to the trader's process from a helper container in the host's pid namespace, so
#   Docker sees a crash and restarts it (`docker kill` counts as a manual stop, spec 0006 verify.md).
# - network cut (D8, D9): firewall rules inside the trader's network namespace, set by a helper
#   container that joins it, drop everything going out or coming in except the Compose network,
#   loopback (Docker's DNS), and NTP. Redis, Postgres, and the startup clock check keep working;
#   Kraken does not. The rules go when the drill restores them or when the trader restarts.
# - live hand tools: `python -m trader.drill` (foreign-order, foreign-position, cancel-stop,
#   extra-stop, show) in a one off trader container, and `control_cli drill-step` (rest_order,
#   rest_order_and_die, open_position, flatten) in the dashboard container.
#
# In demo it pauses entries first (source cli) and waits until the trader is flat, so the paper
# strategy cannot open a position mid drill, and lifts only a pause it set. A live drill run has no
# trading run, so live checks write no `reconciliations` rows; the live evidence is the log lines.
# Needs Docker and python3 (both on the laptop and the VPS). Exit codes: 0 every drill PASS,
# 1 any FAIL or an error, 64 usage.
set -euo pipefail

usage() {
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//' >&2
    exit 64
}

die() {
    echo "restart-drill: $*" >&2
    exit 1
}

[ $# -ge 1 ] || usage
MODE=$1
shift
case $MODE in
demo) ALL=(D1 D8 D9 D10 D11) ;;
live) ALL=(D1 D2 D3 D4 D5 D6 D7 D8 D9 D10 D11 D12) ;;
*) usage ;;
esac
if [ $# -gt 0 ]; then
    DRILLS=("$@")
    for d in "${DRILLS[@]}"; do
        [[ " ${ALL[*]} " == *" $d "* ]] || die "$d is not a $MODE drill (${ALL[*]})"
    done
else
    DRILLS=("${ALL[@]}")
fi

command -v python3 >/dev/null || die "python3 is needed"
if [ -x /usr/local/sbin/kfs-compose ]; then
    [ "$(id -u)" -eq 0 ] || die "run as root on the VPS (sudo $0 ...)"
    compose() { /usr/local/sbin/kfs-compose "$@"; }
else
    cd "$(git rev-parse --show-toplevel)"
    compose() { docker compose "$@"; }
fi

TOOLS=kfs/drill-tools:local
# Polls and waits, in seconds.
START_WAIT=240     # a restart until its start check (node build, Nautilus reconciliation)
FINDING_WAIT=120   # a change on Kraken until a periodic check reports it (interval 60 s)
FLAT_WAIT=90       # a flatten until Kraken is flat
CUT_S=90           # D8: longer than RISK_STALE_QUOTE_S

# The JSON helpers, on python3 so the laptop needs no jq.
#   pyj find k=v ...   stdin JSON lines, print those whose fields all equal (as text)
#   pyj get EXPR       stdin one JSON value as d, print EXPR (text as is, the rest as JSON)
#   pyj test EXPR      stdin one JSON value as d, exit 0 when EXPR is true
PY='
import json, sys
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "find":
    want = dict(a.split("=", 1) for a in args)
    for line in sys.stdin:
        line = line.strip()
        start = line.find("{")
        if start < 0:
            continue
        try:
            j = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(j, dict) and all(str(j.get(k)) == v for k, v in want.items()):
            print(json.dumps(j))
    sys.exit(0)
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
try:
    v = eval(args[0], {"d": d})
except (KeyError, IndexError, TypeError):
    sys.exit(2)  # a snapshot without the field (the trader is not running yet): unreadable, not a traceback
if cmd == "test":
    sys.exit(0 if v else 1)
print(v if isinstance(v, str) else ("" if v is None else json.dumps(v)))
'
pyj() { python3 -c "$PY" "$@"; }

now() { date -u +%s; }

# The trader container (its id stays the same across restarts and compose stop/start).
TRADER_ID=""
trader() {
    if [ -z "$TRADER_ID" ]; then
        TRADER_ID=$(compose ps -aq trader)
        [ -n "$TRADER_ID" ] || die "no trader container: start the stack first"
    fi
    printf '%s\n' "$TRADER_ID"
}

trader_env() { # trader_env NAME DEFAULT
    local value
    value=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$(trader)" | sed -n "s/^$1=//p" | tail -n 1)
    printf '%s\n' "${value:-$2}"
}

started_at() { docker inspect -f '{{.State.StartedAt}}' "$(trader)"; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$(trader)")" = true ]; }
health() { docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$(trader)"; }

# The trader's JSON log lines since epoch second $1 with the fields given.
logs() {
    local since=$1
    shift
    docker logs --since "$since" "$(trader)" 2>&1 | pyj find "$@"
}

# wait_log SECONDS SINCE k=v...: the first matching line, or exit 1 when none came in time.
wait_log() {
    local timeout=$1 since=$2 end line
    shift 2
    end=$(($(now) + timeout))
    while :; do
        line=$(logs "$since" "$@" | head -n 1)
        [ -z "$line" ] || {
            printf '%s\n' "$line"
            return 0
        }
        [ "$(now)" -lt "$end" ] || return 1
        sleep 2
    done
}

# trader_state's data (fresh, under 5 s), through the dashboard's control CLI.
state() {
    compose exec -T dashboard python -m dashboard.control_cli state 2>/dev/null | pyj get 'd["data"]'
}

# wait_state SECONDS EXPR: until trader_state's data (d) makes EXPR true.
wait_state() {
    local end=$(($(now) + $1)) s
    while :; do
        if s=$(state) && pyj test "$2" <<<"$s"; then
            return 0
        fi
        [ "$(now)" -lt "$end" ] || return 1
        sleep 2
    done
}

control() { compose exec -T dashboard python -m dashboard.control_cli "$@"; }

# Kraken's open orders and positions (live only).
# Its boot logs go to stdout too, so the result is the last line.
drill_tool() { compose run --rm --no-deps -T trader python -m trader.drill "$@" 2>/dev/null | tail -n 1; }
kraken() { drill_tool show; }

ENTRIES='[o["venue_order_id"] for o in d["orders"] if not o["reduce_only"]]'
STOPS='[o for o in d["orders"] if o["reduce_only"] and o["order_type"] in ("stop_market", "stop_limit")]'
FLAT='not d["orders"] and not d["positions"]'

# wait_kraken SECONDS EXPR: until Kraken's view (d) makes EXPR true; prints that view.
wait_kraken() {
    local end=$(($(now) + $1)) view
    while :; do
        if view=$(kraken) && pyj test "$2" <<<"$view"; then
            printf '%s\n' "$view"
            return 0
        fi
        [ "$(now)" -lt "$end" ] || return 1
        sleep 3
    done
}

psql_q() {
    # shellcheck disable=SC2016 # expanded by the container's shell
    compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA -c "$1"' sh "$1"
}

# The helper image: alpine with iptables, built once on this host.
tools() {
    docker image inspect "$TOOLS" >/dev/null 2>&1 && return 0
    printf 'FROM alpine:3.22\nRUN apk add --no-cache iptables iptables-legacy\n' | docker build -q -t "$TOOLS" - >/dev/null
}

# SIGKILL the trader's process, so Docker restarts it as after a crash; wait until it runs again.
crash() {
    local before pid end
    before=$(started_at)
    pid=$(docker inspect -f '{{.State.Pid}}' "$(trader)")
    [ "$pid" -gt 0 ] || die "the trader is not running"
    docker run --rm --pid=host "$TOOLS" kill -9 "$pid"
    wait_restart "$before" 60
}

# wait_restart STARTED_AT SECONDS: until the trader runs with a newer start time.
wait_restart() {
    local end=$(($(now) + $2))
    while [ "$(started_at)" = "$1" ] || ! running; do
        [ "$(now)" -lt "$end" ] || return 1
        sleep 0.2
    done
}

# Cut Kraken off: inside the trader's network namespace, keep loopback, the Compose network, and NTP.
CUT='
set -eu
ipt=iptables
$ipt -L -n >/dev/null 2>&1 || ipt=iptables-legacy
cut() { # cut TOOL NETS...
    t=$1
    shift
    $t -N KFS_DRILL_OUT 2>/dev/null || $t -F KFS_DRILL_OUT
    $t -N KFS_DRILL_IN 2>/dev/null || $t -F KFS_DRILL_IN
    $t -A KFS_DRILL_OUT -o lo -j RETURN
    $t -A KFS_DRILL_IN -i lo -j RETURN
    for net in "$@"; do
        $t -A KFS_DRILL_OUT -d "$net" -j RETURN
        $t -A KFS_DRILL_IN -s "$net" -j RETURN
    done
    $t -A KFS_DRILL_OUT -p udp --dport 123 -j RETURN
    $t -A KFS_DRILL_IN -p udp --sport 123 -j RETURN
    $t -A KFS_DRILL_OUT -j DROP
    $t -A KFS_DRILL_IN -j DROP
    $t -C OUTPUT -j KFS_DRILL_OUT 2>/dev/null || $t -I OUTPUT 1 -j KFS_DRILL_OUT
    $t -C INPUT -j KFS_DRILL_IN 2>/dev/null || $t -I INPUT 1 -j KFS_DRILL_IN
}
cut "$ipt" "$@"
cut "${ipt%tables*}6tables${ipt#*iptables}" 2>/dev/null || true
'
UNCUT='
ipt=iptables
$ipt -L -n >/dev/null 2>&1 || ipt=iptables-legacy
for t in "$ipt" "${ipt%tables*}6tables${ipt#*iptables}"; do
    $t -D OUTPUT -j KFS_DRILL_OUT 2>/dev/null
    $t -D INPUT -j KFS_DRILL_IN 2>/dev/null
    $t -F KFS_DRILL_OUT 2>/dev/null && $t -X KFS_DRILL_OUT
    $t -F KFS_DRILL_IN 2>/dev/null && $t -X KFS_DRILL_IN
done
true
'
CUT_ON=0
cut_kraken() {
    local nets
    nets=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}/{{.IPPrefixLen}} {{end}}' "$(trader)")
    [ -n "${nets// /}" ] || return 1
    # shellcheck disable=SC2086 # one network per word
    docker run --rm --net "container:$(trader)" --cap-add NET_ADMIN "$TOOLS" sh -c "$CUT" sh $nets
    CUT_ON=1
}
restore_kraken() {
    [ "$CUT_ON" -eq 1 ] || return 0
    if running; then
        docker run --rm --net "container:$(trader)" --cap-add NET_ADMIN "$TOOLS" sh -c "$UNCUT" || true
    fi
    CUT_ON=0
}

# --- Drill bookkeeping ---

FAILED=()
PASSED=()
DRILL=""
DRILL_OK=1
begin() {
    DRILL=$1
    DRILL_OK=1
    echo
    echo "== $1: $2"
    echo "   expected: $3"
}
evidence() { echo "   evidence: $*"; }
ok() { echo "   ok:   $*"; }
bad() {
    echo "   FAIL: $*"
    DRILL_OK=0
}
end_drill() {
    if [ "$DRILL_OK" -eq 1 ]; then
        echo "   $DRILL PASS"
        PASSED+=("$DRILL")
    else
        echo "   $DRILL FAIL"
        FAILED+=("$DRILL")
    fi
}

# q COMMAND...: the command with its output dropped (check's own lines stay visible).
q() { "$@" >/dev/null 2>&1; }

# Resume, and in demo pause entries again (a resume lifts the pause too, spec 0010 AC-4).
resume() {
    control resume >/dev/null || return 1
    [ "$MODE" = live ] || control pause --source cli >/dev/null
}

# check NAME COMMAND...: ok when the command succeeds, else a FAIL for this drill.
check() {
    local name=$1
    shift
    if "$@"; then ok "$name"; else bad "$name"; fi
}

# The start check after a restart since $1: prints its log line, FAIL when none came.
start_check() {
    local line
    if line=$(wait_log "$START_WAIT" "$1" code=reconcile_check trigger=start); then
        evidence "start check $(pyj get '{k: d.get(k) for k in ("outcome", "reconciliation_id", "exchange_orders", "exchange_positions", "mismatch_count")}' <<<"$line")"
        printf '%s\n' "$line" >"$WORK/start_check"
        return 0
    fi
    bad "no start check within ${START_WAIT}s"
    return 1
}
start_outcome() { pyj get 'd["outcome"]' <"$WORK/start_check"; }

# A finding's log line since $1 of kind $2, within FINDING_WAIT; prints it.
finding() {
    local line
    line=$(wait_log "$FINDING_WAIT" "$1" code=reconcile_mismatch kind="$2") || return 1
    evidence "finding $(pyj get '{k: d.get(k) for k in ("kind", "client_order_id", "action", "action_result", "halts", "event_key")}' <<<"$line")"
    printf '%s\n' "$line"
}

recheck_clean() {
    local line
    line=$(control recheck 2>/dev/null) || {
        evidence "recheck: ${line:-no answer}"
        return 1
    }
    evidence "recheck $(pyj get 'd.get("details")' <<<"$line")"
    pyj test '(d.get("details") or {}).get("outcome") == "clean"' <<<"$line"
}

not_halted() { wait_state 10 'not d["halted"]'; }

# Before a drill: the trader runs, is not halted, and (live) Kraken holds nothing.
ready() {
    running || {
        bad "the trader is not running"
        return 1
    }
    wait_state 30 'not d["halted"] and d["open_positions"] == 0 and d["open_entry_orders"] == 0' || {
        bad "precondition: the trader must be flat, with no entry order and no halt"
        return 1
    }
    if [ "$MODE" = live ]; then
        local view
        view=$(kraken) || {
            bad "precondition: Kraken's view could not be read"
            return 1
        }
        pyj test "$FLAT" <<<"$view" || {
            bad "precondition: Kraken must be flat with no open order: $view"
            return 1
        }
    fi
}

# After a drill: flatten what is left (live), then require flat, no order, no halt.
finish() {
    if [ "$MODE" = live ]; then
        local view
        if view=$(kraken) && ! pyj test "$FLAT" <<<"$view"; then
            control drill-step flatten >/dev/null 2>&1 || true
        fi
        if view=$(wait_kraken "$FLAT_WAIT" "$FLAT"); then
            ok "ends with Kraken flat and no open order"
        else
            bad "Kraken still holds something after ${FLAT_WAIT}s: $(kraken || echo unreadable)"
        fi
    fi
    if wait_state 60 'not d["halted"] and d["open_positions"] == 0 and d["open_entry_orders"] == 0'; then
        ok "ends flat in the trader's books with no halt"
    else
        bad "the trader still holds a position, an entry order, or a halt: $(state || echo unreadable)"
    fi
}

# live: open the drill position and wait for its stop on Kraken; writes the stop to $WORK/stop.
open_position() {
    local line view
    line=$(control drill-step open_position 2>/dev/null) || {
        bad "open_position did not run: ${line:-no answer}"
        return 1
    }
    view=$(wait_kraken 60 "d[\"positions\"] and len($STOPS) == 1") || {
        bad "no position with one stop on Kraken within 60s"
        return 1
    }
    pyj get "$STOPS[0]" <<<"$view" >"$WORK/stop"
    evidence "Kraken position $(pyj get 'd["positions"]' <<<"$view"), stop $(cat "$WORK/stop")"
}
stop_field() { pyj get "d[\"$1\"]" <"$WORK/stop"; }

# --- The drills ---

d1() {
    begin D1 "clean restart while flat" "start check clean, reconciling clears, entries allowed (AC-1, AC-17, AC-23)"
    ready || return 0
    local since id
    since=$(now)
    compose restart trader >/dev/null
    start_check "$since" || return 0
    check "the start check is clean" [ "$(start_outcome)" = clean ]
    check "reconciling and exchange_unknown clear" \
        wait_state 60 'not {"reconciling", "exchange_unknown", "order_unresolved"} & set(d["entries_blocked"])'
    evidence "entries_blocked $(state | pyj get 'd["entries_blocked"]')"
    if [ "$MODE" = demo ]; then
        id=$(pyj get 'd["reconciliation_id"]' <"$WORK/start_check")
        [[ $id =~ ^[0-9a-f-]{36}$ ]] || {
            bad "no reconciliation id in the log line"
            return 0
        }
        local row="" end=$(($(now) + 30))
        while [ -z "$row" ] && [ "$(now)" -lt "$end" ]; do
            row=$(psql_q "SELECT trigger || ' ' || outcome FROM reconciliations WHERE id = '$id'" 2>/dev/null | tr -d '\r')
            [ -n "$row" ] || sleep 2
        done
        evidence "reconciliations row: ${row:-none}"
        check "one reconciliations row, start and clean" [ "$row" = "start clean" ]
    else
        evidence "no reconciliations row expected: a live drill has no trading run"
    fi
}

d2() {
    begin D2 "resting entry across a kill" "start check clean, the drill strategy cancels the order, one order ever on Kraken (AC-1, AC-6, AC-16)"
    ready || return 0
    local line view since entry seen
    line=$(control drill-step rest_order 2>/dev/null) || {
        bad "rest_order did not run: ${line:-no answer}"
        return 0
    }
    view=$(wait_kraken 60 "len($ENTRIES) == 1") || {
        bad "the resting order never showed on Kraken"
        return 0
    }
    entry=$(pyj get "$ENTRIES[0]" <<<"$view")
    evidence "resting order $entry"
    since=$(now)
    crash || {
        bad "the trader did not restart after the kill"
        return 0
    }
    start_check "$since" || return 0
    check "the start check is clean" [ "$(start_outcome)" = clean ]
    seen=$entry
    local end=$(($(now) + 90)) gone=0
    while [ "$(now)" -lt "$end" ]; do
        view=$(kraken) || continue
        seen="$seen $(pyj get "' '.join($ENTRIES)" <<<"$view")"
        if pyj test "not $ENTRIES" <<<"$view"; then
            gone=1
            break
        fi
        sleep 3
    done
    check "the resting order is cancelled" [ "$gone" -eq 1 ]
    check "one entry order ever on Kraken" [ "$(tr ' ' '\n' <<<"$seen" | sed '/^$/d' | sort -u | wc -l | tr -d ' ')" -eq 1 ]
}

d3() {
    begin D3 "lost acknowledgement" "exactly one order on Kraken after the restart, entries blocked until it resolves, no duplicate (AC-11, AC-14, AC-15)"
    ready || return 0
    local before since view seen="" blocked="" end
    before=$(started_at)
    since=$(now)
    control drill-step rest_order_and_die >/dev/null 2>&1 || true # never answers: the trader exits
    wait_restart "$before" 60 || {
        bad "the trader did not exit and restart"
        return 0
    }
    end=$(($(now) + START_WAIT))
    while [ "$(now)" -lt "$end" ] && [ -z "$(logs "$since" code=reconcile_check trigger=start)" ]; do
        blocked="$blocked $(state 2>/dev/null | pyj get '" ".join(d["entries_blocked"])' 2>/dev/null || true)"
        sleep 2
    done
    start_check "$since" || return 0
    evidence "entries blocked before the start check: $(tr ' ' '\n' <<<"$blocked" | sed '/^$/d' | sort -u | tr '\n' ' ')"
    check "entries were blocked (reconciling or order_unresolved) until the check" \
        grep -Eq 'reconciling|order_unresolved' <<<"$blocked"
    end=$(($(now) + 90))
    while [ "$(now)" -lt "$end" ]; do
        view=$(kraken) || continue
        seen="$seen $(pyj get "' '.join($ENTRIES)" <<<"$view")"
        pyj test "not $ENTRIES" <<<"$view" && break
        sleep 3
    done
    local count
    count=$(tr ' ' '\n' <<<"$seen" | sed '/^$/d' | sort -u | wc -l | tr -d ' ')
    evidence "entry orders seen on Kraken: ${seen:-none}"
    [ "$count" -gt 0 ] || bad "no order on Kraken: it never reached Kraken before the process died"
    check "no duplicate entry" [ "$count" -le 1 ]
    check "the order resolves and is gone" pyj test "not $ENTRIES" <<<"${view:-null}"
}

d4() {
    begin D4 "unknown order" "within 60 s an unknown_order finding, cancelled, a critical push (AC-2, AC-10, AC-13)"
    ready || return 0
    local since sent line
    since=$(now)
    sent=$(drill_tool foreign-order) || {
        bad "trader.drill foreign-order failed"
        return 0
    }
    evidence "sent $sent"
    line=$(finding "$since" unknown_order) || {
        bad "no unknown_order finding within ${FINDING_WAIT}s"
        return 0
    }
    check "the action is cancelled, ok" pyj test 'd["action"] == "cancelled" and d["action_result"] == "ok"' <<<"$line"
    check "the finding carries its event key (the alerter pushes it)" pyj test 'd["event_key"].startswith("reconcile:")' <<<"$line"
    check "Kraken has no open order" q wait_kraken 30 'not d["orders"]'
    evidence "check your phone for the critical push"
}

d5() {
    begin D5 "known position across a kill" "adopted, stop unchanged, clean, resume_position called (AC-6, AC-16)"
    ready || return 0
    open_position || return 0
    local since view
    since=$(now)
    crash || {
        bad "the trader did not restart after the kill"
        return 0
    }
    start_check "$since" || return 0
    check "the start check is clean" [ "$(start_outcome)" = clean ]
    check "position_resumed logged" q wait_log 30 "$since" code=position_resumed
    view=$(kraken) || view=null
    check "the same stop rests on Kraken" pyj test \
        "[s['venue_order_id'] for s in $STOPS] == ['$(stop_field venue_order_id)']" <<<"$view"
    check "the position is still there" pyj test 'len(d["positions"]) == 1' <<<"$view"
}

d6() {
    begin D6 "stop removed while down" "missing_stop, the stop placed again at the recorded trigger before anything else (AC-9, AC-13)"
    ready || return 0
    open_position || return 0
    local since line view trigger
    trigger=$(stop_field trigger_price)
    compose stop trader >/dev/null
    evidence "cancelled $(drill_tool cancel-stop || echo failed)"
    since=$(now)
    compose start trader >/dev/null
    start_check "$since" || return 0
    check "the start check is a mismatch" [ "$(start_outcome)" = mismatch ]
    line=$(finding "$since" missing_stop) || {
        bad "no missing_stop finding"
        return 0
    }
    check "the stop is replaced, ok" pyj test 'd["action"] == "stop_replaced" and d["action_result"] == "ok"' <<<"$line"
    view=$(wait_kraken 30 "len($STOPS) == 1") || view=null
    check "one stop on Kraken at the recorded trigger $trigger" pyj test \
        "[s['trigger_price'] for s in $STOPS] == ['$trigger']" <<<"$view"
    check "the next check is clean" recheck_clean
}

d7() {
    begin D7 "unknown position" "unknown_position, flattened, halt reconcile_mismatch, Resume needed (AC-7, AC-13)"
    ready || return 0
    local since line
    since=$(now)
    evidence "sent $(drill_tool foreign-position || echo failed)"
    line=$(finding "$since" unknown_position) || {
        bad "no unknown_position finding"
        return 0
    }
    check "flattened and it halts" pyj test 'd["action"] == "flattened" and d["halts"]' <<<"$line"
    check "halted with reconcile_mismatch" wait_state 30 'd["halted"] and d["halt_reason"] == "reconcile_mismatch"'
    check "Kraken is flat" q wait_kraken "$FLAT_WAIT" "$FLAT"
    sleep 10
    check "still halted until Resume" wait_state 5 'd["halted"]'
    check "Resume answers executed" resume
    check "the halt is lifted" not_halted
    check "the next check is clean" recheck_clean
}

d8() {
    begin D8 "network cut for ${CUT_S}s" "stale_feed, then reconciling; a reconnect check clean when back; the stop on Kraken untouched (AC-3, AC-4)"
    ready || return 0
    [ "$MODE" = demo ] || open_position || return 0
    local since back blocked="" end line view
    since=$(now)
    cut_kraken || {
        bad "could not cut the network"
        return 0
    }
    end=$(($(now) + CUT_S))
    while [ "$(now)" -lt "$end" ]; do
        blocked="$blocked $(state 2>/dev/null | pyj get '" ".join(d["entries_blocked"])' 2>/dev/null || true)"
        sleep 5
    done
    restore_kraken
    back=$(now)
    evidence "entries blocked during the cut: $(tr ' ' '\n' <<<"$blocked" | sed '/^$/d' | sort -u | tr '\n' ' ')"
    check "stale_feed during the cut" grep -q stale_feed <<<"$blocked"
    check "reconciling during the cut" grep -q reconciling <<<"$blocked"
    if line=$(wait_log "$FINDING_WAIT" "$since" code=reconcile_check trigger=reconnect); then
        evidence "reconnect check $(pyj get '{k: d.get(k) for k in ("outcome", "reconciliation_id")}' <<<"$line")"
        check "the reconnect check is clean" pyj test 'd["outcome"] == "clean"' <<<"$line"
    else
        bad "no reconnect check within ${FINDING_WAIT}s after the network came back ($(($(now) - back))s)"
    fi
    check "reconciling clears" wait_state 60 '"reconciling" not in d["entries_blocked"]'
    if [ "$MODE" = live ]; then
        view=$(kraken) || view=null
        check "the same stop rests on Kraken" pyj test \
            "[s['venue_order_id'] for s in $STOPS] == ['$(stop_field venue_order_id)']" <<<"$view"
    fi
}

d9() {
    begin D9 "Kraken unreachable at start" "live: exchange_unknown retries, exit 1 after the start timeout, healthy once back; demo: comes back healthy with a clean check (AC-4, AC-5)"
    ready || return 0
    local since before timeout line healths="" end
    since=$(now)
    cut_kraken || {
        bad "could not cut the network"
        return 0
    }
    before=$(started_at)
    crash || {
        bad "the trader did not restart after the kill"
        return 0
    }
    # The restart made a new network namespace: cut it again before the trader reaches Kraken.
    CUT_ON=0
    cut_kraken || {
        bad "could not cut the network again after the restart"
        return 0
    }
    if [ "$MODE" = live ]; then
        timeout=$(trader_env RECONCILE_START_TIMEOUT_S 300)
        if line=$(wait_log 90 "$since" code=reconcile_start_retry); then
            evidence "retry $(pyj get '{k: d.get(k) for k in ("attempt", "error", "wait_s")}' <<<"$line")"
            ok "the start fetch fails and retries"
        else
            bad "no reconcile_start_retry within 90s"
        fi
        before=$(started_at)
        end=$(($(now) + timeout + 60))
        while [ "$(now)" -lt "$end" ] && [ -z "$(logs "$since" code=reconcile_start_timeout)" ]; do
            healths="$healths $(health)"
            sleep 10
        done
        evidence "health while retrying: $(tr ' ' '\n' <<<"$healths" | sed '/^$/d' | sort | uniq -c | tr '\n' ' ')"
        check "never unhealthy while retrying (the heartbeat stays fresh)" bash -c '! grep -q unhealthy <<<"$1"' sh "$healths"
        check "reconcile_start_timeout after ${timeout}s" q wait_log 5 "$since" code=reconcile_start_timeout
        # The exit restarts the trader in a fresh namespace, so Kraken is back.
        CUT_ON=0
        check "Docker restarts it" wait_restart "$before" 120
        since=$(now)
        start_check "$since" || return 0
        check "the start check is clean" [ "$(start_outcome)" = clean ]
    else
        end=$(($(now) + 90))
        while [ "$(now)" -lt "$end" ]; do
            healths="$healths $(health)"
            sleep 10
        done
        evidence "health while Kraken was cut: $(tr ' ' '\n' <<<"$healths" | sed '/^$/d' | sort | uniq -c | tr '\n' ' ')"
        evidence "a demo start has no Kraken fetch, so no exchange_unknown here (the live drill proves AC-4 and AC-5)"
        restore_kraken
        since=$(now)
        if line=$(wait_log "$START_WAIT" "$since" code=reconcile_check outcome=clean); then
            evidence "check after the network came back $(pyj get '{k: d.get(k) for k in ("trigger", "reconciliation_id")}' <<<"$line")"
            ok "a clean check once Kraken is back"
        else
            bad "no clean check within ${START_WAIT}s once Kraken was back"
        fi
    fi
    check "healthy" bash -c 'for _ in $(seq 60); do [ "$(docker inspect -f "{{.State.Health.Status}}" "$1")" = healthy ] && exit 0; sleep 3; done; exit 1' sh "$(trader)"
}

d10() {
    begin D10 "halted restart" "comes back halted, the restart flatten runs, the start check still runs (AC-1, spec 0010)"
    ready || return 0
    local since
    check "stop answers executed" q control stop
    check "halted" wait_state 30 'd["halted"]'
    since=$(now)
    crash || {
        bad "the trader did not restart after the kill"
        return 0
    }
    check "trader_starts_halted logged" q wait_log 120 "$since" code=trader_starts_halted
    check "restart_flatten logged" q wait_log 120 "$since" code=restart_flatten
    start_check "$since" || true
    check "still halted after the start check" wait_state 10 'd["halted"]'
    check "Resume answers executed" resume
    check "the halt is lifted" not_halted
}

d11() {
    begin D11 "recheck" "recheck answers executed with clean (AC-20)"
    ready || return 0
    check "recheck answers executed, clean" recheck_clean
}

d12() {
    begin D12 "extra stop while down" "stop_size_mismatch, the recorded stop kept, the extra one cancelled, clean on the next check (AC-9, AC-13)"
    ready || return 0
    open_position || return 0
    local since line view
    compose stop trader >/dev/null
    evidence "sent $(drill_tool extra-stop || echo failed)"
    since=$(now)
    compose start trader >/dev/null
    start_check "$since" || return 0
    line=$(finding "$since" stop_size_mismatch) || {
        bad "no stop_size_mismatch finding"
        return 0
    }
    check "its action ran ok" pyj test 'd["action_result"] == "ok"' <<<"$line"
    view=$(wait_kraken 30 "len($STOPS) == 1") || view=null
    check "only the recorded stop is left on Kraken" pyj test \
        "[s['venue_order_id'] for s in $STOPS] == ['$(stop_field venue_order_id)']" <<<"$view"
    check "the next check is clean" recheck_clean
}

# --- Run ---

WORK=$(mktemp -d)
PAUSED_BY_US=0
cleanup() {
    restore_kraken
    if [ "$PAUSED_BY_US" -eq 1 ]; then
        control unpause --source cli >/dev/null 2>&1 || echo "restart-drill: unpause failed, run: control_cli unpause --source cli" >&2
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

env=$(trader_env TRADING_ENV demo)
[ "$env" = "$MODE" ] || die "the trader runs TRADING_ENV=$env, not $MODE"
if [ "$MODE" = live ]; then
    [ "$(trader_env LIVE_MODE off)" = drill ] || die "a live drill needs LIVE_MODE=drill (spec 0023 AC-22)"
fi
running || die "the trader is not running"
tools
echo "restart-drill: $MODE, drills ${DRILLS[*]}, trader $(trader | cut -c1-12)"

if [ "$MODE" = demo ]; then
    if state | pyj test 'not d["paused"]'; then
        control pause --source cli >/dev/null || die "could not pause entries for the drill"
        PAUSED_BY_US=1
    fi
    echo "restart-drill: entries paused, waiting until the trader is flat"
    control wait-flat --timeout 15m >/dev/null || die "the trader did not get flat within 15m"
fi

for d in "${DRILLS[@]}"; do
    # Run under || so one failing command ends that drill's steps, not the whole run.
    "d${d#D}" || bad "the drill stopped on an error"
    finish || bad "the cleanup stopped on an error"
    end_drill
done

echo
echo "restart-drill: PASS ${PASSED[*]:-none} · FAIL ${FAILED[*]:-none}"
[ "${#FAILED[@]}" -eq 0 ]

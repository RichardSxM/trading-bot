#!/usr/bin/env bash
# The fetch scenario of spec 0007 (AC-10), on the laptop with Docker running:
#
#   scripts/tests/fetch-catalog-test.sh
#
# Builds a small catalog with the compactor (the recorder image and its test feed), backs up day one
# and day two in two `catalog` snapshots of a throwaway local restic repository (the backup image),
# fetches both days with scripts/fetch-catalog.sh, and checks that ParquetDataCatalog returns the
# book deltas of each day and the instrument. A fetch of a day with no hours must exit 1.
# Prints PASS, or FAIL with the reason. Removes its temp folder.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fail() {
    echo "FAIL: $*"
    exit 1
}

# Inside the repo: Docker Desktop on the Mac shares /Users reliably, its temp folders not always.
W=$(mktemp -d "$PWD/.fetch-test.XXXXXX")
W=$(cd "$W" && pwd -P)
# restic reads with O_NOATIME, which Docker Desktop's Mac file sharing answers with an I/O error,
# so each snapshot reads a copy in a Docker volume (the VPS reads a real disk).
VOL=kfs-fetch-test-$$
trap 'rm -rf "$W"; docker volume rm -f "$VOL" >/dev/null 2>&1 || true' EXIT
mkdir -p "$W/src" "$W/repo" "$W/secrets" "$W/out"
(umask 077 && printf %s "$(openssl rand -hex 16)" >"$W/secrets/restic_password")
export RESTIC_PASSWORD
RESTIC_PASSWORD=$(cat "$W/secrets/restic_password")
USER_ID="$(id -u):$(id -g)"

docker compose build --quiet recorder backup
BACKUP_IMAGE=$(docker compose config --images | grep 'backup:' | head -n 1)

# py STEP: one step of the catalog fixture, in the recorder image, on $W mounted at /w.
py() {
    docker compose run --rm --no-deps -T --user "$USER_ID" -v "$W:/w" -w /app/apps/recorder \
        --entrypoint python recorder - "$@" <<'EOF'
import sys
import time

sys.path.insert(0, "tests")
from pathlib import Path

from feed_fixture import INSTRUMENT, SECOND_NS, make_feed, write_instruments, write_run
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from recorder.compactor.main import Compactor, run_offline
from recorder.compactor.store import CatalogStore

HOUR_NS = 3_600 * SECOND_NS
DAY_ONE = 1_790_812_800 * SECOND_NS  # 2026-10-01 00:00 UTC
DAY_NS = 24 * HOUR_NS
step = sys.argv[1]


def run(store, name, start):
    btc = TestInstrumentProvider.btcusdt_perp_binance()
    write_instruments(store.live, name, [btc], created_ns=start - SECOND_NS)
    write_run(store.live, name, make_feed(start, start + 90 * 60 * SECOND_NS).messages, rotate_at=[start + HOUR_NS])
    later = lambda: time.time_ns() + 30 * DAY_NS
    assert run_offline(Compactor(store, grace_ns=0, floor_bytes=0, offline=True), store, later) == 0


if step == "day-one":
    run(CatalogStore(Path("/w/src")), "a-dead", DAY_ONE + 6 * HOUR_NS)
elif step == "day-two":
    store = CatalogStore(Path("/w/src"))
    # The VPS retention retired day one before the second snapshot.
    for path in Path("/w/src/data").rglob("2026-10-01T*"):
        path.unlink()
    run(store, "b-dead", DAY_ONE + DAY_NS + 6 * HOUR_NS)
elif step == "check":
    catalog = ParquetDataCatalog("/w/out")
    for day in (DAY_ONE, DAY_ONE + DAY_NS):
        rows = catalog.order_book_deltas(instrument_ids=[INSTRUMENT], start=day, end=day + DAY_NS - 1)
        assert rows, f"no deltas for the day starting {day}"
    ids = {str(i.id) for i in catalog.instruments()}
    assert INSTRUMENT in ids, f"instrument missing: {ids}"
    print("catalog query ok")
EOF
}

restic_run() {
    docker run --rm --user "$USER_ID" -e RESTIC_PASSWORD -v "$W/repo:/repo" -v "$VOL:/data/catalog:ro" \
        --entrypoint restic "$BACKUP_IMAGE" -r /repo --no-cache "$@"
}

snapshot() {
    local paths=() p
    docker run --rm --user 0 -v "$W/src:/s:ro" -v "$VOL:/data/catalog" --entrypoint sh "$BACKUP_IMAGE" \
        -c 'rm -rf /data/catalog/* && cp -R /s/. /data/catalog/ && chmod -R a+rX /data/catalog'
    for p in data quarantine gaps manifests; do
        [ -e "$W/src/$p" ] && paths+=("/data/catalog/$p")
    done
    restic_run backup --quiet --host kfs-vps --tag kfs --tag catalog "${paths[@]}"
}

restic_run init --quiet >/dev/null
py day-one || fail "could not build day one"
snapshot
sleep 1 # two snapshots with distinct times
py day-two || fail "could not build day two"
snapshot

fetch() {
    CATALOG_RESTIC_REPOSITORY="$W/repo" KFS_FETCH_SECRETS="$W/secrets" scripts/fetch-catalog.sh --into "$W/out" "$@"
}

fetch 2026-10-01 2026-10-02 || fail "the two day fetch failed"
[ -n "$(find "$W/out/data" -name '2026-10-01T*' -print -quit)" ] || fail "day one (first snapshot only) not restored"
[ -n "$(find "$W/out/data" -name '2026-10-02T*' -print -quit)" ] || fail "day two not restored"
[ ! -e "$W/out/.fetch-tmp" ] || fail "the temp folder was left behind"
py check || fail "the catalog query failed"

if fetch 2026-10-03 2026-10-03 2>/dev/null; then
    fail "a day with no hours exited 0"
fi
echo PASS

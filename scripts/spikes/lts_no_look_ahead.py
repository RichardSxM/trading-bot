"""Spec 0031 AC-14: one recorded hour, cut at several times, must give the signals of the full run before each cut.

Only the signals are compared. Depth after the signal and markouts look forward by design, so a cut changes them.
Own data only. Run it in the lab image with the catalog and this folder mounted read only:
  KFS_TAG=0031 docker compose --profile lab run --rm --no-deps -T \
    -v <main checkout>/catalog-lab:/catalog-lab:ro -v "$PWD/scripts/spikes:/spike:ro" \
    --entrypoint python lab -I /spike/lts_no_look_ahead.py
"""

import sys
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path

from lab.catalog import SourceTree
from lab.instruments import pinned_instrument
from lab.lts_precheck import SegmentScan, params_of, scan_segment
from lab.lts_runfile import LtsRunFile
from lab.replay import plan_days

RUN_FILE = Path("/app/apps/lab/runs/lts_precheck.toml")
ROOT = Path("/catalog-lab/own")
DAY = date(2026, 10, 9)
SEGMENT = 2
CUTS = (
    datetime(2026, 10, 9, 11, 5, 30, tzinfo=UTC),
    datetime(2026, 10, 9, 11, 40, 0, tzinfo=UTC),
    datetime(2026, 10, 9, 11, 59, 59, tzinfo=UTC),
)


def main() -> int:
    run = LtsRunFile.model_validate(tomllib.loads(RUN_FILE.read_text(encoding="utf-8")))
    instrument = pinned_instrument(run.instrument_id)
    tree = SourceTree(ROOT, run.instrument_id)
    segment = plan_days(tree, [DAY]).segments[SEGMENT]

    def scan(cut_ns: int | None) -> SegmentScan:
        return scan_segment(
            tree,
            segment,
            params=params_of(run, instrument.price_increment.as_decimal()),
            contract_size=instrument.multiplier.as_decimal(),
            size_precision=instrument.size_precision,
            band_bps=run.depth_band_bps,
            with_forced_variant=True,
            cut_ns=cut_ns,
        )

    full = scan(None)
    wrong = 0
    for cut in CUTS:
        cut_ns = int(cut.timestamp()) * 1_000_000_000
        partial = scan(cut_ns)
        for name, variant in full.variants.items():
            expected = [row.signal for row in variant.signals if row.signal.ts_init_ns <= cut_ns]
            got = [row.signal for row in partial.variants[name].signals]
            same = expected == got
            wrong += not same
            print(cut.isoformat(), name, "expected", len(expected), "got", len(got), "same", same, flush=True)
    print("full signals", {name: len(variant.signals) for name, variant in full.variants.items()}, "wrong", wrong)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())

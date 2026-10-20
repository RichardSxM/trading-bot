# 0016 · Tardis trades off the tick

**Status**: Assumed
**Date**: 2026-10-20
**Authorized by**: RichardSxM, during /develop (task 12 of spec 0015, "do everything you recommend")

## Owed decision

What `lab import-tardis` does with a free Tardis day whose trades CSV has prices that are not a multiple of the pinned `PF_XBTUSD` tick. Spec 0008 refuses such a day (no rounding). From 2023-10-01 on, every day tried so far has some: a few bursts a day of many tiny fills (0.0003 to 0.0004 BTC each) at one computed price such as `27567.42278253538`. On 2023-10-01 that is 387 of 11,774 trade rows at 4 timestamps; on 2023-11-01, 300 of 76,738 rows at 3 timestamps. Refusing them leaves only 18 development days, under the 22 day minimum of spec 0015 AC-24, so the `random_taker` replay study cannot run.

## Assumption built on

- The import drops only the trade rows whose price is off the tick. It never rounds one, and it keeps every other value as its original text.
- A book price off the tick still refuses the day, and so does any size off the lot, in the book or the trades.
- The rows are dropped by rewriting the raw trades CSV in place (through a temporary name) before conversion. The warning `lab_off_tick_trades_dropped` names the day, the row count, and up to 3 of the prices, and `lab_import_day_done` gains `off_tick_trades`.
- These fills cannot be the book's own prices, so they cannot be a fill the replay engine would have given a resting order. Dropping them changes no queue position and removes a tiny share of volume.

## Code area

- `apps/lab/src/lab/tardis_import.py` (`check_ticks`, `drop_off_tick_trades`, `import_day`)
- `apps/lab/tests/test_tardis_import.py`

## Requirements

- A Tardis day whose only off tick values are trade prices imports, with those rows gone and the count logged.
- A book price off the tick, or a size off the lot, still exits 1 with `lab_tick_mismatch` and writes no manifest.

## Ratify

This decision was recorded by /develop, not deliberated. Run `/architect strategy scoring & promotion gate: Tardis trades off the tick`
to deliberate and ratify it, and to note it in spec 0008 (AC-1) and spec 0015 (AC-24). Until then it stays flagged as an owed decision; it does not block marking the feature `done`.

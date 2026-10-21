# 0016 · Tardis trades off the tick

**Status**: In Progress (AC-1, AC-2 built in 3e82099; AC-3 and the AC-4 change still to build)
**Date**: 2026-10-20 (assumed during /develop); ratified 2026-10-21 by /architect, which added the 5% guard
**Authorized by**: RichardSxM

## Summary

From October 2023, every free Tardis day has a few bursts of tiny trades at prices that are not on the price grid (the tick). Spec 0008 refused such a day whole, which left too few days for a replay study. The import now drops only those trade rows, logs how many, and keeps everything else as it was. A day where more than 5% of the trade rows are off the tick is still refused, because that looks like a broken file, not tiny fills.

## Context

`lab import-tardis` converts free Tardis days for `PF_XBTUSD` into the lab catalog (spec 0008 AC-1). It refuses a day with any price off the pinned tick or any size off the lot, and it never rounds, so a replay only ever sees values the exchange could have produced.

From 2023-10-01 on, every day tried has trade rows at one computed price per burst (for example `27567.42278253538`), each fill 0.0003 to 0.0004 BTC. On 2023-10-01 that is 387 of 11,774 trade rows (3.3%) at 4 timestamps. On 2023-11-01 it is 300 of 76,738 rows (0.39%) at 3 timestamps. The book on those days is clean.

Refusing those days leaves 18 development days, under the 22 day minimum of spec 0015 AC-24, so no replay study can run. Any rule must still fail closed on a feed that is genuinely corrupt.

## Requirements

**User stories**:

- As the owner, I want replay studies to use every free Tardis day since 2022 so that the gate has enough days to judge a strategy.
- As the owner, I want a broken Tardis file still refused so that a replay never runs on bad data.

**Acceptance criteria**:

- **AC-1**: A Tardis day whose only off tick values are trade prices imports. Those trade rows are removed before conversion, every other row keeps its original text, and nothing is rounded. The book check (`check_ticks`) runs first, so a day that fails both rules logs only `lab_tick_mismatch`. The warning `lab_off_tick_trades_dropped` names the day, the row count, and up to 3 distinct prices, and `lab_import_day_done` carries `off_tick_trades` (0 when none).
- **AC-2**: A book price off the tick, or any size off the lot (book or trades), still exits 1 with `lab_tick_mismatch` and writes no manifest (spec 0008 unchanged).
- **AC-3**: When the off tick trade rows are more than 5% of the day's trade rows (data rows of the raw trades CSV, header excluded; the test is `off × 100 > total × 5` in whole numbers; a day with no trade rows imports), the import drops nothing, logs `lab_off_tick_trades_excess` at error with the day, the count, the total, and up to 3 prices, exits 1, and writes no manifest. Exactly 5% still imports.
- **AC-4**: Rerunning an imported day still skips it (`lab_import_day_skipped`), and a day killed partway converts again from scratch to the same bytes, as in spec 0008. The raw trades CSV is never changed: the kept rows go to a filtered copy beside it (written through a temporary name, then renamed), and conversion reads the copy. So a rerun of a killed day counts the same off tick rows again, and a kill during the write leaves no half file. Both files are deleted with the other raw files once the manifest exists.

## Options considered

### Option 1: Drop the off tick trade rows

Remove only those rows, keep the rest verbatim.
**Pros**: never invents a price; the book, and so every queue position, is untouched; about 0.4% to 3% of rows on the days seen.
**Cons**: the replay loses a sliver of real traded volume; a corrupt file could hide behind it without a cap.

### Option 2: Refuse the whole day (spec 0008 as written)

**Pros**: strictest; no new rule.
**Cons**: no replay study can run (18 of 22 days); the bad rows are not even fills a resting order could get.

### Option 3: Round each price to the nearest tick

**Pros**: keeps all volume.
**Cons**: makes up trade prices that never happened, and those fake prints would then trigger fills against resting orders in the replay.

## Decision

**Chosen option**: Option 1: drop the off tick trade rows, with a 5% guard.

The import removes trade rows priced off the tick, refuses the day when they exceed 5% of its trade rows, and still refuses any book price off the tick and any size off the lot.

## Rationale

A trade at a price no order can rest at cannot be a fill the replay engine would have given us, so removing it changes no simulated fill. Rounding would do the opposite: it creates prints on the tick that can fill a resting order in the replay. Refusing the day protects nothing extra and blocks the gate entirely. The 5% cap keeps the fail closed rule for a file whose trades are broadly wrong: the worst real day seen is 3.3%, so 5% leaves headroom without letting a broken feed through.

## Feature design

**Data model**: none. No schema change; the manifest format is unchanged.

**Flow** (`import_day`, per day): download, then `check_ticks` (book prices, all sizes; refuses as before), then `drop_off_tick_trades` counts trade rows off the tick. Over 5% → `lab_off_tick_trades_excess`, exit 1. Otherwise it writes the kept rows to a filtered copy (temporary name, then rename) and conversion reads that copy, as in spec 0008. A malformed price that is not a number is not off the tick, it is a parse error, and refuses the day as before.

**Value sourcing**:

| Action | Value | Source |
| --- | --- | --- |
| drop check | tick | the pinned instrument's `price_increment` |
| drop check | share of rows | off tick data rows over all data rows (header excluded) of that day's raw trades CSV; 0 rows → share 0 |
| refuse | limit | constant `MAX_OFF_TICK_TRADE_PERCENT = 5` in `tardis_import.py`, compared as `off × 100 > total × MAX_OFF_TICK_TRADE_PERCENT` |
| logs | prices | the first 3 distinct off tick price strings, as written in the CSV |

**Key invariants**: no price or size is ever rounded; book rows are never dropped; the share is integer math, never floats; the raw download is never modified.

**Configuration required**: none.

**Critical test scenarios**:

- A day with 2 off tick trades out of 100 imports with 98 trades and logs both events, verifies **AC-1**.
- An off tick book price still refuses, verifies **AC-2**.
- 6 of 100 off tick refuses with `lab_off_tick_trades_excess` and no manifest; 5 of 100 imports, verifies **AC-3**.

## Build plan

1. Already built (3e82099): `drop_off_tick_trades`, the warning, `off_tick_trades` on `lab_import_day_done`. Satisfies **AC-1**, **AC-2**.
2. Add the 5% guard: count before writing, refuse above the limit with `lab_off_tick_trades_excess`, and tests at 5 and 6 of 100 and at 0 trades. Write the filtered copy instead of rewriting the raw CSV, with a test that a second run counts the same rows. Satisfies **AC-3**, **AC-4**.

## Consequences

**Positive**:

- Replay studies have every free day since 2022; the `random_taker` study ran end to end.
- No made up price ever enters the catalog.

**Negative / tradeoffs**:

- Replay volume is a little below the real feed on affected days, so any fill model that leans on trade prints sees slightly fewer of them.
- A day with 5% to 100% bad trades now needs a person to look at it; a future feed change could start refusing days.
- Days imported before this change were refused, not imported; the AC-1 rule applies only to imports run after 3e82099.

## Follow-up

- [ ] Spec 0008 AC-1 and spec 0015 AC-24 carry a pointer to this spec (done with this ratification).

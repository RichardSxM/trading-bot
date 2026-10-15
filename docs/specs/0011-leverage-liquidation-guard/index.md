# 0011. Size every entry from its risk, cap exposure and leverage, keep liquidation far away, and count funding

**Date**: 2026-10-12
**Status**: Accepted

## Summary

Every entry is now sized from what it would lose if its stop hit: 0.5 % of equity, counting the stop distance, the slippage past the stop, the taker fees on both legs, and the funding the trade would pay if it is held across an hourly funding time. The caps still apply on top (order notional, a new total exposure cap in USD, and leverage at 2x), and the smaller size wins.

Before the order goes in, the guard estimates where the exchange would liquidate the account (force close the position because margin ran out). It refuses the entry when that price is closer than 10 % to the entry, or not at least 3 times further away than the stop. Every refusal is recorded and logged like the existing ones.

In demo, the trader now books a funding payment at every hourly funding time a position is open. The payment goes into `funding_payments`, the position's `funding_total`, the loss limits, and the dashboard's P&L. The "before funding" label goes away. Live funding from Kraken's account log is left for go live (feature 19). Nothing in the schema changes.

## Requirements

**User stories**:
- As the owner, I want every stopped out trade to cost about the same small share of the account, so one wide stop cannot do the damage of ten tight ones.
- As the owner, I want hard ceilings on order size, total exposure, and leverage that no strategy can talk its way past, so a sizing bug stays small.
- As the owner, I want the bot to refuse any entry whose liquidation price sits too close, so the stop always closes the position long before the exchange would.
- As the owner, I want funding counted before a trade and booked after it, so a trade that looks good before funding is not a loser after it, and my P&L and loss limits are true.

**Acceptance criteria** (the contract, each criterion is IDed and independently checkable):

*Sizing*

- **AC-1** (risk size): `check_entry` sizes the entry when the request carries a stop price and no fixed quantity. The loss per unit (USD per contract) is the sum of:
  - **stop distance** `|entry_px − stop_px|`;
  - **slippage** `stop_px × RISK_STOP_SLIPPAGE_BPS ÷ 10 000`;
  - **fees** `taker_fee × (entry_px + stop_px)` (the entry and the stop exit are both market orders);
  - **funding** `funding_periods × max(0, cost_sign × rate) × entry_px`, where `cost_sign` is +1 for a buy and −1 for a sell (a long pays a positive rate, a short pays a negative one), and a funding rate the trade would earn counts as 0.

  The risk size is `equity × RISK_PER_TRADE ÷ loss per unit`, rounded down to the instrument's size step. `entry_px` is the mid. `taker_fee` is the strategy config's `GuardedConfig.taker_fee`, the fee the venue charges each fill: `SANDBOX_TAKER_FEE` in demo, the run's taker fee in the lab. Never `instrument.taker_fee`: Kraken publishes its instruments with fee 0, and the sandbox puts its fee only on its own exchange's copy. One value feeds both the guard and the fills, so the two cannot drift apart: in demo, the sandbox and `placeholder_config` both read `settings.sandbox_taker_fee`; in the lab, the engine's instrument copy and `RunContext.taker_fee` both come from `plan.taker_fee`. The trader builds only the placeholder config, and only on its demo paper path. `RandomTaker` is built only in the lab. Live has no fee source yet, so `placeholder_config` raises for any env but demo. It runs while the node is being put together, before the node starts, and the env guard refuses live as well (owed to feature 19, see Follow-up). `rate` is the relative funding rate per hour, the `ContextBook` value as the lab uses it.
- **AC-2** (caps clip, smaller wins): the entry quantity is the smallest of the risk size and the three cap sizes, each rounded down to the size step:
  - **order notional:** `RISK_MAX_ORDER_NOTIONAL_USD ÷ entry_px`;
  - **total exposure:** `(RISK_MAX_TOTAL_EXPOSURE_USD − open_notional) ÷ entry_px`;
  - **leverage:** `(RISK_MAX_LEVERAGE × equity − open_notional) ÷ entry_px`.

  The verdict says which one bound (`limited_by`: `risk`, `notional_cap`, `exposure_cap`, or `leverage_cap`), compared after rounding. On a tie a cap wins over `risk`, and among caps the first in the AC-11 order wins. When the result is zero (below one size step), the entry is refused with `zero_quantity` if `risk` bound, else with the binding cap's reason. A headroom below zero counts as zero.
- **AC-3** (fixed quantity callers): a request that carries `fixed_quantity` (the lab's `RandomTaker`) is not sized. Its quantity is checked against the same three caps in the order notional, exposure, leverage, and the first one broken refuses it, as today.
- **AC-4** (exposure counts every instrument): `open_notional` is the sum over every open position in the cache, any instrument, of `|signed qty| × that instrument's mid`, before this order. A position whose instrument has no quote yet makes the inputs unjudgeable (AC-15).

*Liquidation*

- **AC-5** (liquidation estimate): the guard estimates the liquidation price `liq_px` of the account after the order fills at `entry_px`, as cross margin (the whole account backs the position). With equity `E`, entry quantity `q`, the other open notional `O`, the entry fee `f = taker_fee × q × entry_px` (`taker_fee` per AC-1), and maintenance rate `m` (AC-6):
  - **buy:** `liq_px = (q × entry_px + f + m × O − E) ÷ (q × (1 − m))`
  - **sell:** `liq_px = (E + q × entry_px − f − m × O) ÷ (q × (1 + m))`

  A buy whose estimate is zero or below has no liquidation price (`liq_px` null, distance infinite).
- **AC-6** (maintenance rate): `m` is the larger of the instrument's `margin_maint` (as NautilusTrader's Kraken instrument carries it; 0 when unset) and `RISK_MAINTENANCE_MARGIN_RATE`.
- **AC-7** (too close): with `liq_distance = |entry_px − liq_px|` and `stop_distance = |entry_px − stop_px|`, the entry is refused with:
  - `liq_too_close` when `liq_distance ÷ entry_px` is below `RISK_MIN_LIQ_DISTANCE_PCT ÷ 100`;
  - `liq_inside_stop` when `liq_distance` is below `RISK_LIQ_STOP_MULTIPLE × stop_distance` (skipped when the request has no stop price).

  Both use the final quantity from AC-2 or AC-3.
- **AC-8** (open position alarm): every position snapshot (every `SNAPSHOT_INTERVAL_NS`, 60 s) stores in `position_snapshots.liquidation_px` the AC-5 estimate for the open position, with `entry_px` = the snapshot's mark (never the average entry), `E` = the full current equity including all unrealized P&L, `q` = the absolute quantity, the side from the position, `f` = 0 (on purpose: the entry fee is already paid and sits in equity), and `O` = the other open notional. When the mark is closer to it than `RISK_MIN_LIQ_DISTANCE_PCT`, the trader writes one `critical` `limit_hit` risk event keyed `liq_near:<position row id>` (idempotent on its key, so once per position, restarts included). A null estimate, or a missing mid on another open instrument, leaves `liquidation_px` null and raises no alarm. It takes no action; the stop protects the position.

*Funding in the decision*

- **AC-9** (funding periods): `funding_periods` is the number of funding boundaries (the top of each UTC hour, `FUNDING_PERIOD_NS`) in `(now, now + max_hold]`, where `now` is the decision time and `max_hold` is the strategy's planned longest hold (a late fill is accepted as an approximation). A request with no `max_hold` counts as 1 period. `RandomTaker` passes `funding_periods = 0` (a lab probe, no market context).
- **AC-10** (stale funding refuses): when `funding_periods` is above 0 and the strategy's `ContextBook` funding rate is missing or stale (spec 0009 AC-9, `CONTEXT_MAX_AGE_FUNDING_S`), the entry is refused with `funding_stale`. With `funding_periods` at 0 the rate is not needed and not checked.

*Order and record of a refusal*

- **AC-11** (refusal order): `check_entry` returns the first refusal in this order: `halted`, `paused`, `blocked:<reason>`, `position_open`, `funding_stale`, `zero_quantity`, `notional_cap`, `exposure_cap`, `leverage_cap`, `liq_too_close`, `liq_inside_stop`. The new reasons are `warning` refusals (`refusal_severity`), except `funding_stale`, which is `info` (a feed state, not a limit).
- **AC-12** (everything recorded): every entry decision, approved or refused, records in its `inputs` JSON the figures the guard used: `equity`, `entry_px`, `stop_px`, `open_notional`, `taker_fee`, `maintenance_rate`, `funding_rate` (or null), `funding_periods`, `loss_per_unit`, `risk_quantity`, `quantity`, `limited_by`, `liq_px` (or null), `liq_distance_pct`. For a fixed quantity caller, `stop_px`, `loss_per_unit`, and `risk_quantity` are null. A refusal also writes the existing `limit_hit` risk event keyed `limit_hit:<decision id>` with `{reason}` in its details, as today.

*Funding in P&L (demo)*

- **AC-13** (booking): at each funding boundary, for each open position in the paper run, the trader writes one `funding_payments` row with `source = computed`:
  - **amount** `−signed_qty × mid × rate` (negative when paid), in the instrument's settle currency;
  - **rate** is the last `FundingRateUpdate` at or before the boundary, as `trading_core.lab.funding.rate_at` picks it, and it must be newer than the boundary minus `FUNDING_PERIOD_NS`;
  - **mid** is the latest quote's mid at the boundary, younger than `RISK_STALE_QUOTE_S`;
  - **keys** are the paper run, the instrument, `paid_ns` = the boundary, and the position's row id.

  **Who is charged.** A position is charged when `opened_ns < boundary` and it is still open or `closed_ns > boundary`, judged by the position's own event timestamps, not by cache state when the alert runs. So a position that opens or closes exactly at a boundary is not charged for it.

  **The alert.** The `RecordingActor` arms a time alert (`clock.set_time_alert_ns`) for the next top of the UTC hour in `on_start`, and arms the next one each time it fires. It cancels the alert's name before setting it again: a live clock still holds a firing alert's name while its callback runs, so setting the same name raises. Boundaries missed while the trader was down are never booked. The actor subscribes to `FundingRateUpdate` for its instruments itself and keeps the latest rate with its `ts_init` per instrument.

  **Repeats.** Each row is built once, and the spool replays that same row, so writing it twice leaves one row (spec 0002 AC-5). When no usable rate or quote exists, nothing is booked, and one `warning` `limit_hit` risk event keyed `funding_unbooked:<instrument>:<paid_ns>` is written.
- **AC-14** (position total): `positions.funding_total` is the sum of that position's `funding_payments.amount`, updated in the same transaction as each payment insert, only when the insert writes a new row (`record_funding` returns whether it inserted). `positions.realized_pnl` keeps NautilusTrader's figure (net of fees, before funding). The net P&L of a position is `realized_pnl + funding_total`, everywhere it is shown or counted. The position upsert never overwrites `funding_total`: the column is left out of its update set (an insert still writes 0).
- **AC-15** (inputs that cannot be judged): `check_entry` raises `InvalidRiskInput` when the mid or equity is zero or below, the stop price is on the wrong side of the mid or equal to it, the size step is zero or below, or an open position's instrument has no mid. The caller logs `risk_input_invalid` and skips the entry (fail closed), as today.
- **AC-16** (loss limits count funding): the spec 0010 loss figures add every booked funding payment as realized, by its `paid_at` UTC day (today) and on or after `RISK_TOTAL_LOSS_SINCE` (total). At start, the seed reads them from `funding_payments` of the env, along with their keys. After that, each booking reaches the `LossLimitActor` as a message, counted once per `(instrument_id, paid_ns, position row id)`. Messages that arrive before the seed lands are held and folded after it, as position events are (spec 0010 AC-13). Every fold runs under the actor's lock and is followed by an evaluation, so a payment can trip a limit while flat. Postgres is the source of truth at seed time; a booking whose write later fails stays counted until the next restart (fail closed: it can only make the loss look larger).
- **AC-17** (equity in demo): the equity the guard sizes from is the sandbox balance plus the open positions' unrealized P&L plus the sum of this run's booked funding (the sandbox balance never sees funding). `FundingLedger` seeds that sum from `SUM(funding_payments.amount)` of the run at start, then adds each booking.

*Dashboard and config*

- **AC-18** (dashboard): the Overview position card and the Trades page drop "before funding". Closed round trips show net P&L (`realized_pnl + funding_total`) and a funding column. The open position card shows the funding paid so far. The paper P&L figures on Overview include funding: `dashboard.data.pnl` sums `realized_pnl + funding_total` over closed positions plus `funding_total` over open ones.
- **AC-19** (config): the trader refuses to start when:
  - `RISK_PER_TRADE` is not in (0, 0.02];
  - `RISK_MAX_TOTAL_EXPOSURE_USD` is zero or below, or below `RISK_MAX_ORDER_NOTIONAL_USD`;
  - `RISK_MAINTENANCE_MARGIN_RATE` is not in (0, 0.5);
  - `RISK_MIN_LIQ_DISTANCE_PCT` is not in (0, 100);
  - `RISK_LIQ_STOP_MULTIPLE` is below 1;
  - `RISK_STOP_SLIPPAGE_BPS` is not in [0, 500].

  All new settings carry the `risk_` prefix, so they land in the strategy version config.
- **AC-20** (end to end on the VPS demo): after deploy, the placeholder's entries are sized by the guard and recorded with the AC-12 figures, every position snapshot has a `liquidation_px`, and the dashboard shows P&L without "before funding". A funding booking at a real hourly boundary is proven by the engine level test of AC-13 (the placeholder never holds across the hour, see Consequences).

## Decision

**Chosen option**: Option 2: extend the pure `check_entry` into the single guard (sizing, caps, liquidation, funding term), book demo funding in the trader's recording actor, and keep funding in its own column.

`check_entry` stays the one function every strategy calls, now returning the quantity it sized along with the verdict. The liquidation and sizing maths are pure functions in `trading_core.risk`. The trader's `RecordingActor` books funding at each hourly boundary, because it already holds each position's row id, the write queue, and a timer.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`) · `sqlalchemy` (`terminalskills/skills`, `.claude/skills/sqlalchemy/`) · `postgres-writing-guidelines` (`damusix/skills`, `.claude/skills/postgres-writing-guidelines/`) · `htmx` (`.claude/skills/htmx/`)

**The calls this spec makes** (each with the runner up):
- **`check_entry` sizes as well as judges.** `EntryRequest` gains `stop_px`, `fixed_quantity`, `size_step`, `taker_fee`, `maintenance_rate`, `funding_rate`, and `funding_periods` (`open_notional` now covers every instrument, AC-4), and `RiskVerdict` gains `quantity`, `limited_by`, and a frozen `GuardFigures` (the AC-12 values). One pure call returns everything the strategy records.
  - **Runner up:** a separate `size_entry` before `check_entry`. Two calls that must agree on the same inputs, and a stale funding rate would have to be handled in both.
- **Caps clip, the guard still refuses.** Sizing never builds an order that breaks a cap. The refusals stay for fixed quantity callers and for zero headroom, so "an order that breaks a cap is rejected" holds for every caller.
  - **Runner up:** refuse instead of clip. You chose clip, which keeps the strategy trading at the largest size the caps allow.
- **Entry price is the mid.** The market entry pays about half the spread, which is tiny next to the stop distance and is covered by the slippage allowance.
  - **Runner up:** the touch price (ask for a buy, bid for a sell). Slightly more exact, but the existing caps already use the mid, and two prices in one check is a source of bugs.
- **A funding rate the trade would earn counts as 0.** Never let expected funding income make a trade bigger.
  - **Runner up:** count it signed. More exact on average, but it would grow the size on a forecast.
- **`funding_stale` only matters when a boundary falls inside the hold.** The placeholder (10 minute hold on a 15 minute grid) never crosses the hour, so a stale funding feed must not stop it.
  - **Runner up:** refuse on a stale rate always. Simpler, but it blocks trades that pay no funding.
- **Funding booked by the `RecordingActor`**, on a time alert at each top of the hour, with the rate from its own `FundingRateUpdate` subscription (latest rate and `ts_init` per instrument, so the AC-13 age rule can be checked) and the mid from the cache. It publishes each booked payment on the message bus topic `kfs.funding.booked`. A small `FundingLedger` (plain Python, `node/funding.py`) keeps the run's booked total for the strategy's equity.
  - **Runner up:** a separate funding actor. It would need the position row ids that only the recording actor holds.
- **`funding_total` is bumped inside `record_funding`**, in the same transaction, only when the insert wrote a new row (`RETURNING id` came back). The position upsert drops `funding_total` from its update set. This keeps the column correct across restarts and repeated writes.
  - **Runner up:** the mapper writes an in memory sum on every upsert. A restart or an upsert racing a booking would overwrite it.
- **`realized_pnl` stays before funding** (your call in the interview), matching the replay lab ([apps/lab/src/lab/replay.py](../../../apps/lab/src/lab/replay.py)). Spec 0002's sign note ("`realized_pnl` is net of fees and funding") is corrected by this spec.
- **`PLACEHOLDER_NOTIONAL_USD` is removed.** The placeholder passes its stop price (`stop_price` at the mid) and `max_hold = PLACEHOLDER_HOLD_MIN` instead. `RandomTaker` keeps its `notional_usd` and passes `fixed_quantity`, with no stop price (it places no stop).
  - **Runner up:** keep the setting as a fourth cap. It would duplicate `RISK_MAX_ORDER_NOTIONAL_USD`.
- **Fees come from the strategy config** (`GuardedConfig.taker_fee`), set by whoever builds the config: the trader from `SANDBOX_TAKER_FEE` (the fee the sandbox charges), the lab from the run's taker fee. The trader's `placeholder_config` refuses any env but demo until feature 19 decides the live source. (Corrected 2026-10-15, PR #41: this spec first said `instrument.taker_fee`, which is 0 for every Kraken instrument in the node cache, so verify step 2 showed entries sized without fees.)
  - **Runner up:** `instrument.taker_fee`. One source for every mode in theory, but Kraken publishes 0, the sandbox fee never reaches the cache, and the real live fee depends on the account's fee tier, not the instrument.

## Feature design

**Data model sketch** (no migration; the spec 0002 schema already has every slot):
- `funding_payments`: new `source = computed` rows from the paper run. `position_id` is always set for these rows. Unique on `(run_id, instrument_id, paid_ns)` where `source = 'computed'`. One position per instrument at a time, so the key cannot collide between positions.
- `positions.funding_total`: now non zero; written only by `record_funding`.
- `positions.realized_pnl`: unchanged meaning (before funding).
- `position_snapshots.liquidation_px`: now filled for every snapshot of an open position.
- `decisions.inputs`: gains the AC-12 figures.
- `risk_events`: new keys `liq_near:<position row id>` and `funding_unbooked:<instrument>:<paid_ns>`, kind `limit_hit`.

**State transitions**: none new. The strategies' round trip phases are unchanged; a refusal leaves the strategy flat, as today.

**Interface surface** (no HTTP surface; pure functions and node wiring):

| Interface | Where | Inputs | Outputs | Errors |
|---|---|---|---|---|
| `check_entry(request, limits)` | `trading_core.risk.check` | `EntryRequest` (existing fields, plus `stop_px`, `fixed_quantity`, `size_step`, `taker_fee`, `maintenance_rate`, `funding_rate`, `funding_periods`), `RiskLimits` (plus `risk_per_trade`, `max_total_exposure`, `min_liq_distance_pct`, `liq_stop_multiple`, `stop_slippage_bps`) | `RiskVerdict(approved, reason, quantity, limited_by, figures)` | `InvalidRiskInput` (AC-15) |
| `risk_quantity(...)`, `cap_quantity(...)` | `trading_core.risk.sizing` | equity, risk, loss per unit; caps, mid, open notional, size step | quantity, `limited_by` | `ValueError` on bad inputs |
| `liquidation_price(side, q, entry_px, equity, other_notional, fee, m)` | `trading_core.risk.liquidation` (new, pure) | as in AC-5 | `Decimal \| None` | `ValueError` on q ≤ 0 or m outside (0, 1) |
| `funding_periods(now_ns, max_hold_ns)` | `trading_core.risk.liquidation` or `risk.funding` | now, hold | int | none |
| `funding_amount(signed_qty, mid, rate)` | reuse `trading_core.lab.funding` rule | | `Decimal` | none |
| `record_funding(session, row)` | `trading_core.recording.repository` | `FundingRow` with `position_id` | none; bumps `positions.funding_total` on a new row | `RecordConflictError`, `MissingParentError` (unknown position) |
| `WriteCall.RECORD_FUNDING` | `trading_core.recording.spool` | `FundingRow` | queued write | spool rules as today |
| `kfs.funding.booked` message | `RecordingActor` → `LossLimitActor` | instrument, paid_ns, position row id, amount | folded into the loss tally | none |

**Value sourcing**:

| Action | Value produced / displayed | Source |
|---|---|---|
| entry check | `entry_px` | mid of `cache.quote_tick(instrument)` (as today) |
| entry check | `stop_px` | placeholder: `trading_core.strategy.placeholder.stop_price(mid, side, PLACEHOLDER_STOP_PCT, price_increment)`; RandomTaker: none |
| entry check | `equity` | AC-17: `account.balance_total(USD)` + open positions' unrealized at their mid + `FundingLedger.run_total` |
| entry check | `open_notional` | AC-4: every `cache.positions_open()`, `|signed qty| × mid` of its instrument |
| entry check | `taker_fee` | `GuardedConfig.taker_fee`: trader `SANDBOX_TAKER_FEE` (demo only), lab the run's `taker_fee`; live refused until feature 19 |
| entry check | `maintenance_rate` | AC-6: max(`instrument.margin_maint`, `RISK_MAINTENANCE_MARGIN_RATE`) |
| entry check | `funding_rate`, staleness | strategy `market_context().funding_rate` (`ContextValue.value`, `.is_stale`), spec 0009 AC-8/9 |
| entry check | `funding_periods` | AC-9 from `clock.timestamp_ns()` and `max_hold` (placeholder `PLACEHOLDER_HOLD_MIN`, which already runs with `use_market_context=True`); RandomTaker passes 0 |
| entry check | `size_step` | `instrument.size_increment` |
| entry check | `quantity`, `limited_by`, `liq_px`, `loss_per_unit` | derived in `check_entry` (AC-1, AC-2, AC-5) |
| entry check | caps | `RISK_PER_TRADE`, `RISK_MAX_ORDER_NOTIONAL_USD`, `RISK_MAX_TOTAL_EXPOSURE_USD`, `RISK_MAX_LEVERAGE`, `RISK_MIN_LIQ_DISTANCE_PCT`, `RISK_LIQ_STOP_MULTIPLE`, `RISK_STOP_SLIPPAGE_BPS` |
| snapshot | `liquidation_px` | AC-8: `liquidation_price` with entry = snapshot mark, full current equity, fee 0 (the entry fee is already in equity), other open notional |
| booking | `paid_ns` | the boundary the time alert was set for (top of the UTC hour) |
| booking | `rate` | the actor's own latest `FundingRateUpdate` per instrument, at or before the boundary, within one `FUNDING_PERIOD_NS` |
| booking | `mid`, `signed_qty` | latest quote at the boundary, younger than `RISK_STALE_QUOTE_S`; `position.signed_decimal_qty()` |
| booking | who is charged | `position.ts_opened` and `ts_closed` against the boundary (AC-13) |
| demo equity | run funding sum | `FundingLedger`, seeded from `SUM(funding_payments.amount)` of the run, then each booking |
| booking | `position_id`, `run_id`, `currency` | `RecordingActor._position_ids[(position_ref, opened_ns)]`; the paper run id; `instrument.settlement_currency` |
| position | `funding_total` | sum of its `funding_payments.amount` (AC-14) |
| loss limits | funding per day and total | `funding_payments.amount` by `paid_at` (seed), then `kfs.funding.booked` (AC-16) |
| dashboard | net P&L, funding column, funding paid so far | `positions.realized_pnl + positions.funding_total`; `positions.funding_total` |

**Key invariants**:
- No entry order is submitted with a quantity above the smallest of the risk size and the three cap sizes.
- A refusal never raises; only unjudgeable inputs raise, and the caller then skips the entry.
- `positions.funding_total` always equals the sum of its `funding_payments.amount`.
- `realized_pnl` means the same thing in paper, live, and replay runs: NautilusTrader's, before funding.
- No money value is a float; all maths is `Decimal`, rounded down to the size step only at the end.

**Security model**: no new surface. The trader writes `funding_payments` and updates `positions` with grants it already has (spec 0002: `trader_app` may UPDATE `positions`, and `funding_payments.position_id` from NULL only). The dashboard only reads. Nothing here touches secrets or live keys.

**Configuration required** (in `PaperLoopSettings`, validated at start, AC-19):
- `RISK_PER_TRADE`: fraction of equity a stopped trade may lose, default `0.005`
- `RISK_MAX_TOTAL_EXPOSURE_USD`: ceiling on summed open notional, default `500`
- `RISK_MAINTENANCE_MARGIN_RATE`: maintenance margin floor, default `0.01` (check against Kraken's margin schedule, see Follow-up)
- `RISK_MIN_LIQ_DISTANCE_PCT`: minimum distance from entry to liquidation, default `10`
- `RISK_LIQ_STOP_MULTIPLE`: liquidation at least this many stop distances away, default `3`
- `RISK_STOP_SLIPPAGE_BPS`: slippage allowance past the stop, default `5`
- Removed: `PLACEHOLDER_NOTIONAL_USD`

**Critical test scenarios** (each maps to an acceptance criterion):
- Risk binds: equity 1000, risk 0.5 %, stop 0.3 %, fees and slippage give the exact quantity rounded down; verifies **AC-1**.
- Fee source: an instrument with `taker_fee` 0 in the cache and a config with 0.0005 sizes with 0.0005 and records it; a live trader config refuses to build; verifies **AC-1**, **AC-12**.
- The funding alert set again while it is still armed replaces it, and books the next boundary without `recording_failed`; verifies **AC-13**.
- A cap binds: the same with a 250 USD order cap gives `250 ÷ mid` rounded down and `limited_by = notional_cap`; zero exposure headroom refuses with `exposure_cap`; verifies **AC-2**, **AC-4**.
- Fixed quantity over a cap is refused in the stated order; verifies **AC-3**.
- Liquidation: a long and a short at 2x on 1000 USD match hand worked values; a long at 1x has no liquidation; maintenance from the instrument wins when larger; verifies **AC-5**, **AC-6**.
- High leverage settings refuse `liq_too_close`; a wide stop with a near liquidation refuses `liq_inside_stop`; no stop skips the buffer rule; verifies **AC-7**.
- A hold crossing one boundary with a positive rate adds the funding term for a long and not for a short; a stale rate refuses only when a boundary is inside the hold; verifies **AC-9**, **AC-10**.
- Refusal order: several conditions at once give the first in the list; verifies **AC-11**.
- Engine level (NautilusTrader TestClock, sandbox, real `RecordingActor`): a position open across a boundary books one row with the right sign; booking twice keeps one row and `funding_total` unchanged; open or close exactly at the boundary books nothing; no rate books nothing and writes `funding_unbooked`; verifies **AC-13**, **AC-14**.
- Integration (real Postgres): `record_funding` bumps `funding_total` once, and a later position upsert leaves it alone; verifies **AC-14**.
- Loss tally: a funding payment today pushes the daily loss over the limit and trips it; the seed counts stored payments once; verifies **AC-16**.
- Bad config refuses start, one case per rule; verifies **AC-19**.

## Build plan

Ordered as tracer bullets: first one thin thread through the real entry path, then thicken each part end to end.

1. Settings: add the six `RISK_*` settings with their validators, remove `PLACEHOLDER_NOTIONAL_USD`, satisfies **AC-19**.
2. Pure sizing and caps: extend `EntryRequest`, `RiskLimits`, `RiskVerdict` (with `GuardFigures`), add `risk_quantity` and `cap_quantity`, the new refusal order and severities, and the `InvalidRiskInput` cases, with unit tests, satisfies **AC-1**, **AC-2**, **AC-3**, **AC-11**, **AC-15**.
3. Wire the thin thread: the placeholder passes its stop price, size step, taker fee (`GuardedConfig.taker_fee`, AC-1), open notional over every instrument, and `max_hold`, submits the verdict's quantity, and records the AC-12 figures; `RandomTaker` passes `fixed_quantity`, satisfies **AC-4**, **AC-12**.
4. Pure liquidation: `liquidation_price`, the maintenance rate rule, both distance refusals in `check_entry`, with hand worked unit tests, satisfies **AC-5**, **AC-6**, **AC-7**.
5. Snapshot `liquidation_px` and the `liq_near` alarm in the `RecordingActor`, satisfies **AC-8**.
6. Funding term: `funding_periods`, the strategy's `ContextBook` rate into the request, the `funding_stale` refusal, satisfies **AC-9**, **AC-10**.
7. Funding writes: `WriteCall.RECORD_FUNDING`, `record_funding` bumping `funding_total` on a new row, the position upsert no longer writing `funding_total`, with integration tests, satisfies **AC-14**.
8. Funding booking in the `RecordingActor`: the hourly time alert, rate and mid at the boundary, `funding_unbooked`, the `FundingLedger`, the `kfs.funding.booked` message, and the engine level test, satisfies **AC-13**, **AC-17**.
9. Loss limits: seed funding from `funding_payments`, fold `kfs.funding.booked` once per key, satisfies **AC-16**.
10. Dashboard: net P&L and the funding column on Trades, funding paid on the position card, Overview P&L with funding, the label removed, and its tests, satisfies **AC-18**.
11. Verify on the VPS demo with a `verify.md` (deploy, read decisions, snapshots, dashboard), satisfies **AC-20**.

## Consequences

**Positive**:
- One function still answers "may this entry go, and how big": every future strategy gets sizing, caps, liquidation, and funding for free.
- Paper P&L and the loss limits now include funding, so a strategy that holds across hours cannot hide its funding cost.
- `realized_pnl` means the same thing in every mode, so replay and paper results stay comparable.

**Negative / tradeoffs**:
- With today's settings (1000 USD, 0.3 % stop, 250 USD order cap), the risk size is around 1100 USD, so the order cap binds and the placeholder keeps trading at about 250 USD. Risk sizing only starts to matter with wider stops or higher caps.
- The placeholder never holds across the hour, so in demo the funding term is always 0 and no funding row is booked on the VPS. The booking is proven by the engine level test, not by a live demo hour, until a strategy holds longer (features 16, 17).
- The liquidation estimate holds the other positions' notional fixed and ignores the exit fee: exact for one instrument, an approximation once ETH trades too.
- A position shorter than the 60 s snapshot interval gets no `liquidation_px` and no alarm; the entry check already covered it.
- The liquidation estimate is our formula, not Kraken's. It is pessimistic (the maintenance rate floor), but a change in Kraken's margin rules would not reach it until the setting is updated.
- The guard counts the taker fee on both legs and ignores maker fills. That only makes it more careful: the placeholder's entry and stop are both market orders, and a maker fill would cost less.
- More inputs on every entry decision means a larger `inputs` JSON and more fields a strategy must supply.

**Neutral**:
- No migration. Spec 0002's sign note on `realized_pnl` is corrected here; `/sync` should flag it.
- The strategy version hash changes (new `risk_` settings, one removed), so the next deploy starts a new strategy version.

## Follow-up

- [ ] Live funding: poll Kraken's account log for funding entries (`source = kraken_log`), owed before feature 19 goes live; the live trader should refuse to start without it.
- [ ] Live taker fee source, owed to feature 19 (its `/architect` run decides it). Until then `placeholder_config` refuses live. Recommended: read the account's fee tier from Kraken's futures fee schedule (the public schedules plus the account's 30 day volume; endpoint names to confirm then) at start, and refuse to start when it cannot be read. Runner up: a validated `RISK_TAKER_FEE` setting set to the highest tier's fee, simpler, but safe only while someone keeps it at or above the real tier by hand (a higher fee only makes entries smaller). Whichever wins, also settle these:
  - **Move the live refusal** out of `placeholder_config` into one shared factory that builds every `GuardedConfig`, so no future live strategy can skip it.
  - **Keep up with the tier.** It moves with 30 day volume, so read it again during a run, or size with a margin above it.
  - **Bound the fee.** Give it a validated range with an upper sanity cap. An absurdly high fee would quietly size every entry to zero.
- [ ] Before go live, check `RISK_MAINTENANCE_MARGIN_RATE` and `instrument.margin_maint` against Kraken's current margin schedule for `PF_XBTUSD`, and compare one live position's reported liquidation price with the AC-5 estimate.
- [ ] Spec 0002 Follow-up (how demo funding reaches `funding_payments`) is settled by this spec; correct its sign note on `realized_pnl`.
- [ ] Spec 0006's "before funding" P&L note is superseded by AC-18.
- [ ] Measure the real slippage past stops in the replay lab and tune `RISK_STOP_SLIPPAGE_BPS`.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

# 0034. Trend vol ramp: scale in entries with a wide stop

**Date**: 2026-11-24
**Status**: In Progress

## Summary

`trend_vol_ramp` is `trend_vol` with two changes. It enters a new or flipped position over three daily rebalances (a third of the target each day), and it closes a coin that moves 8 daily volatilities against its entry, then keeps it flat until the trend signal changes side. Cuts and closes still happen at once, and only the 00:05 UTC daily run may add size, so a crash restart or an unpause never buys at a random moment. It needs no new table: the ramp is worked out from the position you already hold, and stops are rebuilt from the recorded decisions after a restart.

## Requirements

**User stories**:
- As the operator, I want a new trend position built over three days so that one badly timed entry costs at most a third of the position.
- As the operator, I want a crash restart or an unpause to never buy at whatever price that moment has, so that OOM restarts stop setting my entry prices.
- As the operator, I want a coin that goes badly against me closed and kept out until its trend turns, and to hear about it.

**Acceptance criteria**:

- **AC-1** (pure ramp rule, `trading_core/strategy/trend_vol_ramp.py`): `RampParams` (`steps` 3, `stop_vols` 8) refuses `steps` below 1 or `stop_vols` at or below 0. `ramp_target(full, current, steps, may_grow)` (both signed USD notionals, Decimal) returns the working target:
  - `full` is 0 → 0.
  - `current` on the other side of `full`, or 0 → `sign(full) × |full| / steps` when `may_grow`, else 0.
  - `current` on `full`'s side and `|current| ≥ |full|` → `full` (a cut happens at once).
  - `current` on `full`'s side and `|current| < |full|` → `sign(full) × min(|full|, |current| + |full| / steps)` when `may_grow`, else `current` (hold).
  With `steps` 1 it returns `full` whenever `may_grow`. No I/O, no Nautilus, no float.
- **AC-2** (pure stop rule, same module): `stop_hit(side, entry_px, mark, entry_vol, stop_vols)` is true when `side × (mark / entry_px − 1) ≤ −stop_vols × entry_vol` (side +1 long, −1 short); it raises `ValueError` on an entry price or mark at or below 0, or a side other than ±1. `entry_vol` 0 never hits. `stop_holds(signals, stopped_side)` takes the coin's signal (−1, 0, +1, or None for not eligible) for each daily plan day after the stop day, oldest first. It is true only while every one equals `stopped_side`, so an empty list holds.
- **AC-3** (node, `trading_core/node/trend_vol_ramp.py`): `TrendVolRamp(TrendVol)`, `STRATEGY_NAME = "trend_vol_ramp"`, `ORDER_ID_TAG = "TVR"`, kind `trend`. Everything `TrendVol` does is inherited unchanged: book vol sizing, band, schedule, orders, flips, retries, decisions, seeds, start and resume rebalances. In addition:
  - `_make_plan` sets `full_plan` to the sized plan only after `super()._make_plan` returns True (a failed plan leaves `full_plan` as it was).
  - The node overrides `_rebalance(reason, skip)`. Before calling `super()._rebalance`, it builds the round's working plan from `full_plan` (never from `self.plan`, so rounds never compound) with `dataclasses.replace` per `CoinPlan`. Each coin's `target_notional` becomes `ramp_target(full, qty × mid, steps, may_grow)`, and `weight` scales with it. It then assigns the working plan to `self.plan`. `may_grow` is `reason == trend_daily.DAILY`. A stopped coin (AC-5) gets 0.
  - A hold is never encoded as a target. In a round that is not daily, a coin with a position on its target's side below full, or a flat coin with a nonzero target, joins the `skip` set passed to `super()._rebalance` (`ramp_hold_grow`). That way price rounding can never send a stray cut. A flip in such a round has working target 0 (close only).
  - "Flat" means `_qty(coin) == 0`.
  - The working plan is frozen for the round, so retries, flip open legs, and the grows fallback (all of which read `self.plan`) never add a second step.
  - A coin with a nonzero position and no mid at round start is skipped that round (`ramp_no_mid`), never sized blind. A flat coin needs no mid.
  - When a step's quantity rounds below the instrument's `min_quantity`, the node targets `min_quantity` on that side (capped at the full target's quantity) and logs `ramp_step_min_qty`, so a small coin still builds.
  - The band check (inherited, first step only) runs against the working target.
  - It logs one `ramp_target` line per coin per round (reason, full, current notional, working, may_grow).
- **AC-4** (hourly stop check):
  - **Timer**: armed in an `on_start` override after `super().on_start()`, at the next full hour (`(now // 3600 s + 1) × 3600 s`) as a one shot `_alert`. The callback re-arms the next hour first, as `_on_daily` does. An `on_stop` override cancels it (the base's `_cancel_all_timers` lists names explicitly).
  - **Gating**: it does nothing until the base's `_started` is true. It still runs while paused or halted, because a stop only reduces.
  - **Which coins**: each coin with an open position on side s. A coin with any order of this strategy open or in flight (`orders_open` plus `orders_inflight`) is skipped this hour.
  - **Entry price**: the position's `avg_px_open`, converted with `Decimal(str(...))`.
  - **Entry vol**: `sample_vol` (`strategy/trend_portfolio.py`) on the coin's closes from `closes_until(open_day − 1 day)` over `vol_days`, where `open_day` is the UTC day of `ts_opened`. It needs at least `vol_days + 1` closes. Otherwise it uses today's `full_plan` vol for the coin and logs `ramp_entry_vol_fallback`.
  - **Mark**: the coin's mid. No mid, or a quote older than `stale_quote_s`, means the coin is not checked this hour (`ramp_stop_mark_stale`).
  - **When `stop_hit` is true**: it records a decision (intent `exit`, reason `stop_vol`; inputs side, entry_px, entry_vol, mark, stop_vols, current_qty, plan_day). It marks the coin stopped on side s at submit, with the current `plan_day`. It sends a reduce only market close for the whole position through `_submit` with no `track` and no `coin_exit`, so the base's dollar exit re-entry is never armed. It logs `ramp_stop_hit` (AC-8).
  - **A refused, denied, or expired stop close**: retried after `retry_wait_s` while the coin is still stopped and held, not left for the next hour.
  - **Errors**: a check that raises logs `ramp_stop_check_failed` for that coin, and the other coins are still checked.
- **AC-5** (stop block): a stopped coin's working target is 0 in every round. Overrides of `_step` and `_send_grow` return without sending anything that would grow a stopped coin, and cancel its retry timer, so a grow queued or retried before the stop can never undo it. Clearing runs only in the daily run (an `_on_daily` path, never the start or start later plans): a stopped coin whose new plan has a later `plan_day` than the stop's and a signal that differs from the stopped side (0, the other side, or not eligible) is cleared (`ramp_stop_cleared`). That same daily round then treats it as any other coin, so a nonzero signal on the other side enters a third. A pause, flatten, or kill switch does not clear a stop.
- **AC-6** (restart): on start, `trader/main.py` reads, for this `trader_id` and `env` across all runs, the newest `stop_vol` decision per coin (new query `latest_decisions_by_coin(session, env, trader_id, reason)` in `recording/queries.py`, returning instrument, `decided_ns`, `side`). The stopped side is long when the recorded side is `SELL`, short when `BUY`. `TrendVolRamp` takes them as `stops` (`trend_vol_ramp.py` never imports `recording.queries`) and, after the seeds load, rebuilds the coin's signal for each plan day d whose daily run time (day d + 1 at `run_at_utc`) is after the stop's `decided_ns`, up to the newest seed day. Each day's signal comes from `portfolio_plan(closes_until(d), Decimal(1), params)` (the same rule, no parallel signal function), but with `min_history_days` lowered to `lookback_days + 1` for the rebuild only, because only 150 closes are kept. A day that is still not eligible counts as unknown, not as a change. It keeps the stop when every known signal equals the stopped side (`ramp_stop_restored`), and drops it when any known signal differs. When the seeds do not reach back to the stop day, or any day is unknown, it keeps the stop (fail closed, `ramp_stop_restored` with `partial: true`). A read failure refuses to start, as the seed read does.
- **AC-7** (visibility): `plan_snapshot` keeps `TrendVol`'s fields, patching each coin dict the base builds so `target_notional` comes from `full_plan` and `target_qty` is recomputed with `target_quantity(full, mid, size_increment)` (the full target), and adds per coin `held_pct` (current notional over the full target times 100, 2 places; None when the target is 0 or there is no mid) and `stopped_side` (`long`, `short`, or None), plus top level `ramp_steps` and `stop_vols`. On the dashboard's portfolio plan view (`live/portfolio.py` `_rows`, a step after `signal_words`, which stays unchanged), a stopped coin's signal reads `Stopped (long)` or `Stopped (short)`, and a coin with a target and `held_pct` under 70 shows `Building <held_pct>%`. Older snapshots without these fields render as today.
- **AC-8** (alert): a new alert rule `ramp_stop_hit`, tier warning, fires on the `ramp_stop_hit` log code, keyed by code and instrument so each coin's stop pushes once. `ramp_stop_check_failed` joins the `portfolio_run_failed` rule's code list.
- **AC-9** (wiring and hashes): `PAPER_STRATEGY=trend_vol_ramp` builds it over every `PAPER_INSTRUMENTS` coin with constants `TVR_*` equal to the `TV_*` ones plus `TVR_RAMP_STEPS = 3` and `TVR_STOP_VOLS = 8`. The version records every setting. Wiring touches: `paper_strategy.py`'s name lists (`_MULTI_COIN`, `SEEDED_STRATEGIES`, near lines 80, 194, 205), a new version branch beside the `trend_vol` one, a `_trend_vol_ramp_settings`, and `build_trend_vol_ramp`; `main.py`'s `BOOK_STRATEGIES`, the build dispatch, and the seed fetch check (near line 1208, which today compares with `trend_vol` only); and the test asserting `BOOK_STRATEGIES` (`test_paper_trend_daily.py`). All new code lives in new files, apart from `trader/paper_strategy.py`, `trader/main.py`, `recording/queries.py`, `alerts/rules.py` (and its promises list), and the dashboard plan view. Before editing them, the build confirms none of them is in any registered version's closure (`versioning.py`). A test proves every existing strategy hash is unchanged. If a base class hook turns out to be needed, stop and come back to `/architect`.
- **AC-10** (rollout): on the VPS demo, after pause, flatten, deploy, and a promote you run, the main trader runs `trend_vol_ramp`. The first 00:05 UTC run logs `ramp_target` for all 12 coins with working targets at a third of full (the book starts flat), and the hourly check logs no `ramp_stop_check_failed`.

## Decision

**Chosen option**: Option 1: stateless grow cap plus a recorded stop, as a new `trend_vol_ramp` strategy.

Each daily rebalance may add at most a third of a coin's target to what it holds, only the daily run may add, and an 8 vol stop from the position's average entry is checked hourly and rebuilt from decision rows after a restart.

## Feature design

**Data model**: no schema change. Stops are `decisions` rows (`reason = 'stop_vol'`, `intent = 'exit'`, `side` = the closing order side). The ramp has no stored state: it reads the held quantity.

**State per coin (in memory)**: `stopped_side: Side | None`. Set by AC-4, cleared by AC-5, rebuilt by AC-6.

**State transitions** (per coin):
`flat → building` (daily run, nonzero signal, a third in) → `building → full` (two more daily runs) → `full`. A cut happens at once in any state. A signal flip closes at once and starts `building` on the new side the same day. `any → stopped` (stop hit, flat) → `stopped → flat` (a daily plan whose signal is not the stopped side) → `building`.

**Interface surface** (no HTTP):
| Surface | Inputs | Outputs | Errors |
|---|---|---|---|
| `ramp_target(full, current, steps, may_grow)` | Decimal USD, int, bool | working target, Decimal USD | none |
| `stop_hit(side, entry_px, mark, entry_vol, stop_vols)` | int, Decimal ×4 | bool | `ValueError` on bad input |
| `stop_holds(signals, stopped_side)` | sequence of int or None, int | bool | none |
| `latest_decisions_by_coin(session, env, trader_id, reason)` | | `{instrument: (decided_ns, side)}` | DB errors propagate (start refused) |
| `TrendVolRamp(config, gate, blocks, record, seeds, last_start_ns, stops)` | `stops`: `{instrument: (day, side)}` | | config `ValueError` |
| Snapshot `{env}:snapshot:portfolio_plan` | | AC-7 fields | |
| Log codes | | `ramp_target`, `ramp_hold_grow`, `ramp_no_mid`, `ramp_stop_hit`, `ramp_stop_cleared`, `ramp_stop_restored`, `ramp_entry_vol_fallback`, `ramp_stop_check_failed` | |

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| Round start | full target | `full_plan.coins[coin].target_notional` (`book_vol_plan`, spec 0033) |
| Round start | current notional | `_qty(coin)` × `guard.mid` |
| Round start | may_grow | the rebalance reason equals `trend_daily.DAILY` |
| Round start | steps | `TVR_RAMP_STEPS` via config |
| Stop check | side, entry price | the Nautilus position: sign of `signed_decimal_qty()`, `avg_px_open` |
| Stop check | entry vol | `sample_vol` on `closes_until(ts_opened UTC day − 1)` over `vol_days`, fallback today's `full_plan` vol |
| Stop check | mark | `guard.mid` at H:00, refused when older than `stale_quote_s` |
| Stop check | plan_day at stop | `self.plan_day` when the stop is sent |
| Stop check | stop_vols | `TVR_STOP_VOLS` via config |
| Stop clear | today's signal | `full_plan.coins[coin].signal` and `eligible` |
| Restart | stopped coins | `latest_decisions_by_coin(..., "stop_vol")` → day from `decided_ns`, side from the order side |
| Restart | signals since the stop | each day's plan signal rebuilt from the seed closes with `portfolio_plan`'s rule |
| Snapshot | held_pct | current notional / full target × 100 |

**Key invariants**:
- A round that is not daily never makes a coin's position larger in absolute size or opens one.
- One daily round adds at most `|full| / steps` per coin, including all its retries.
- A stopped coin is never grown until a daily signal leaves the stopped side.
- Book sizing (`gross`, `unit_vol`) is computed on the full plan, so ramping and stopped coins only lower exposure.
- Money and prices stay Decimal; `avg_px_open` is converted at the edge.

**Security model**: no new access. The dashboard reads the snapshot only; nothing new is written to `control`.

**Configuration required**: the new `PAPER_STRATEGY` value `trend_vol_ramp`; no new env vars (the `TVR_*` constants are code, recorded in the version).

**Critical test scenarios**:
- Ramp from flat: three daily rounds at a steady target give 1/3, 2/3, then full; a start round on a flat book sends nothing. Verifies **AC-1**, **AC-3**.
- Retry in a round: a grow refused then retried in the same daily round still ends at one third, not two. Verifies **AC-3**.
- Flip: long full → short signal closes at once and opens a third short in the same daily round. Verifies **AC-1**, **AC-3**.
- Cut: a lower target on a resume round cuts at once; a higher one holds. Verifies **AC-1**, **AC-3**.
- Stop: a long falls 8 entry vols at H:00 → reduce only close, `stop_vol` decision, `ramp_stop_hit` alerted; following daily rounds with the same signal keep it at 0; a 0 signal clears it. Verifies **AC-2**, **AC-4**, **AC-5**, **AC-8**.
- Restart: a recorded stop with an unchanged signal since is restored; one whose signal changed since is dropped; one older than the seeds is kept. Verifies **AC-6**.
- Stop race: a grow queued or on retry when the stop fires never sends; a refused stop close retries after `retry_wait_s`. Verifies **AC-4**, **AC-5**.
- Hold: a resume round with a coin at a third holds it through `skip` (no order at all, at any mid). Verifies **AC-3**.
- Position fields: `ts_opened` and `avg_px_open` stay correct after a ramp step, a cut, and a flip under netting. Verifies **AC-4**.
- Restart off by one: a stop at 00:00 and a stop at 12:00 count the right first plan day. Verifies **AC-6**.
- Hashes: every registered strategy hash is unchanged. Verifies **AC-9**.

## Build plan

Tracer Bullet: a thin real thread first (the ramp alone, wired and running in the node), then thicken with the stop, restart, and visibility.

1. [x] Pure rule: `RampParams`, `ramp_target`, `stop_hit`, `stop_holds` with unit tests, satisfies **AC-1**, **AC-2**.
2. [x] Thin thread: `TrendVolRamp` with `full_plan`, the per round working plan, and `may_grow` only on daily; `TVR_*` constants and `PAPER_STRATEGY` wiring after the closure check; node tests for ramp, retry, flip, cut; hash test, satisfies **AC-3**, **AC-9**.
3. [x] Stop: the hourly H:00 timer, entry vol, the `stop_vol` decision and close, the block and the clear on the daily plan, satisfies **AC-4**, **AC-5**.
4. [x] Restart: `latest_decisions_by_coin` (integration test on Postgres), wiring in `trader/main.py`, signal rebuild from seeds, satisfies **AC-6**.
5. Visibility and alert: snapshot fields, dashboard plan view words, the `ramp_stop_hit` rule and the `portfolio_run_failed` list, satisfies **AC-7**, **AC-8**.
6. Rollout on the VPS demo (pause, flatten, deploy, promote you run, unpause) and the first run checks, satisfies **AC-10**.

## Migration plan

**Strategy**: switch on the main trader's demo slot, no data migration.
**Phases**:
1. Merge and deploy with `PAPER_STRATEGY` still `trend_vol` (code ships dark; the hash test proves nothing changed).
2. Pause, flatten, set `PAPER_STRATEGY=trend_vol_ramp` in `/etc/kfs/kfs.env`, deploy, run the promote, unpause. The book stays flat until the next 00:05 UTC.
**Rollback**: set `PAPER_STRATEGY=trend_vol` and redeploy with the same pause and flatten steps.
**Risks**: a missing promote refuses the start (`paper_promotion_required`); a deploy killed midway leaves a pause in place (vps-access notes).

## Consequences

**Positive**:
- A bad entry moment costs a third of a position, and restarts and unpauses no longer pick entry prices.
- No migration and no new stored state; a restart rebuilds everything from positions, closes, and decision rows.
- `trend_vol` and its gate record stay frozen; switching back is one setting.

**Negative / tradeoffs**:
- After any restart or unpause on demo, the book stays flat until the next 00:05 UTC (01:05 Amsterdam), then needs three days to be full. A crash at 00:30 UTC means about 23.5 hours out of the market.
- New trends are caught later and at a third of the size at first; in a fast trend, that costs return.
- The lab test helped at 00 and 06 UTC but was slightly worse at 12 and 18 (precheck.md). The live 00:05 run sits at the hour where it helped, but the evidence is soft and was seen before the test.
- The stop measures from the average entry, which later thirds move, so it sits slightly tighter than the tested first slice stop. The stop added nothing in the test; it is a safety net, not an edge.
- On live, a reconciled position's `ts_opened` may be the reconcile time, so entry vol could come from a later day than the real open.
- It does not fix the OOM kills (trader-oom-2026-11-21); it only makes them cheaper.

**Neutral**:
- Feature 35 (per coin close and hold) will share the "coin held at 0 until something clears it" idea; its design should reuse the stop block path.

## Follow-up

- [x] Enroll this as a scope feature (feature 36) linking this spec.
- [ ] Feature 35's `/architect`: reuse the AC-5 block for a manual hold, and decide whether a resume press may grow outside the daily run (this spec says only the daily run grows).
- [ ] After 30 demo days, compare `trend_vol_ramp`'s worst open loss per entry against `trend_vol`'s demo record.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

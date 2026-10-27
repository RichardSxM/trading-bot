# 0021. `imbalance_fade`: fade a lopsided order book with post only orders

**Date**: 2026-10-27
**Status**: Proposed

## Summary

Spec 0017's `book_imbalance` bet that price drifts toward the thin side of a lopsided order book, and it lost money on average before any fee (gross −0.95 USD a trade). Published research on live maker orders finds the same thing and says the contrarian side is where maker orders can work. This spec tests the exact mirror, `imbalance_fade`: the same signal, the opposite side, post only in and out, in the replay lab. A cheap pre check on 4 days `book_imbalance` never ran on decides whether the 40 hour study runs at all, and the gate stays exactly as it is.

## Requirements

**User stories**:
- As the trader, I want the one scalping signal that was reliably wrong tested from the other side, with maker fees only, so I learn cheaply whether it is a real edge.
- As the trader, I want a quick pre check that stops the run before 40 hours of replay when the idea cannot reach the gate's trade floor or cover its cost.

**Acceptance criteria**:
- **AC-1** (hash baseline): before any code, `verify.md` records the sorted `module hash` list sha256 of every `trading_core.node` module at the commit this feature's branch starts from (before any 0021 code), from `scripts/spikes/hash_check.py` (spec 0020 AC-1 method). At close out (AC-9) the same list on the branch matches it line for line, and only `trading_core.node.imbalance_fade` is added.
- **AC-2** (pure rule): `trading_core/strategy/imbalance_fade.py` holds `ImbalanceFadeState`, a subclass of `BookImbalanceState` with the same constructor, that inherits `on_trade` unchanged and overrides only `on_firing`: it calls `super().on_firing(...)`, and when that returns a `ScalpSignal` it returns the mirror: side `BUY` becomes `SELL` with stop `mid × (1 + stop_bps ÷ 10,000)` and target `mid × (1 − target_bps ÷ 10,000)`, and side `SELL` becomes `BUY` with stop `mid × (1 − stop_bps ÷ 10,000)` and target `mid × (1 + target_bps ÷ 10,000)`, where `mid` is the parent signal's `inputs["mid"]`. `inputs` is passed through unchanged. No signal stays no signal. `trading_core/strategy/book_imbalance.py` is not edited.
- **AC-3** (node): `trading_core/node/imbalance_fade.py` holds `STRATEGY_NAME = "imbalance_fade"`, `ImbalanceFadeConfig` (a subclass of `BookImbalanceConfig`, declared `frozen=True, kw_only=True`, with the same defaults), and `ImbalanceFade`, a subclass of `BookImbalance` that sets `self.rule` to an `ImbalanceFadeState` built from the config and whose `entry_reason` is `imbalance_fade_entry`. Everything else (the L2 book and trade subscriptions, the 15 second timer, the base's maker entry, the maker target exit of spec 0017 AC-19, the stop, the time stop) is inherited unchanged. `trading_core/node/book_imbalance.py` is not edited.
- **AC-4** (registry): `apps/lab/src/lab/strategies.py` registers `imbalance_fade` with kind `scalping`, `run_fields=_scalp_fields`, `default_entry_mode="maker"`, its own factory `_imbalance_fade` that raises `TypeError` unless `isinstance(config, ImbalanceFadeConfig)`, and no `candle_screen` or `candle_gate` (replay only, as `book_imbalance`). Registry tests list it in sorted order.
- **AC-5** (pre check run): one scratch run file `lab-reports/scratch/imbalance_fade_precheck.toml` (never a study) holds exactly these keys (the same as `book_imbalance`'s local pre check file, which is git ignored): `source = "tardis"`, `instrument_id = "PF_XBTUSD.KRAKEN"`, `seed = 7`, fees 0.0002 and 0.0005, `latency_ms = 25`, `starting_balance_usd = "10000"`, `leverage = "2"`, `[risk] max_order_notional = "2000"`, `max_leverage = "2"` (every other risk limit at its default), `[strategy] name = "imbalance_fade"`, and in `[strategy.config]` `time_stop_min = 5`, `entry_mode = "maker"`, `exit_mode = "maker"`, `entry_timeout_s = 10`, `min_target_bps = "6"`, `max_spread_bps = "6.27"`, `levels = 5`, `persist_bars = 2`, `stop_bps = "9"`, and the most permissive grid point `imb_min = "0.3"`, `target_bps = "6"`. `lab replay` runs it once per day over **2022-11-01, 2023-11-01, 2024-11-01, and 2025-08-01**: development days `book_imbalance` never ran on (the AC-7 study reads them again later, so they are fresh only against the result that inspired this rule). All four are already in `catalog-lab`. `verify.md` records per day the run id, approved `enter` count (the run's `decisions` rows with `intent` `enter` and `risk_verdict` `approved`, read from the laptop Postgres), closed trades, net P&L (after fees, funding left out), and fees.
- **AC-6** (pre check verdict): pooled over the 4 days, the pre check passes only when both hold: closed trades a day, Σ `trade_count` ÷ 4, is at least (≥) 25 (so 12 holdout days give about 300, leaving room above `[replay] min_holdout_trades` 200 for a chosen grid point with `imb_min` 0.5, which trades less); and gross P&L per trade, (Σ `net_pnl` + Σ `fees_total`) ÷ Σ `trade_count`, is at least 1.5 × cost per trade, Σ `fees_total` ÷ Σ `trade_count` (funding left out, as spec 0017 AC-23). On a fail, `verify.md` and the Results section record "not run: pre check" with both figures, no study file is written, no study runs, no revision is allowed, and the feature goes to close out (AC-9).
- **AC-7** (study, only after a pass): `apps/lab/studies/imbalance_fade.toml` is written and committed only after AC-6 passes. It is the replay study shape of `apps/lab/studies/delta_divergence.toml` (`from = 2022-04-01`, `to = 2025-10-18`, `seed = 7`, the same fees, latency, balance, leverage and `[risk]`), the AC-5 fixed settings in `[strategy.config]`, `[strategy.grid] imb_min = ["0.3", "0.5"]`, `target_bps = ["6", "9"]` (4 points), and `[study] name = "imbalance_fade"`, `lab = "replay"`, `entry_mode = "maker"`, `seed = 42`. The image is rebuilt with `GIT_COMMIT` first (study files are baked in), and `test_scalp_studies.py` is updated to expect the file. It runs `lab study`, `lab holdout` (its holdout days are every 1st from 2025-11-01 up to the last one before the run date: 12 if it runs before 2026-11-02, 13 from then on; either way the gate's 200 trade floor applies), `scripts/push-bundles.sh`, and `gate evaluate imbalance_fade` once on the VPS with `--no-deps`. The gate thresholds (v1) are not changed.
- **AC-8** (results record): after `gate evaluate`, the Results section below gets, in its own commit `docs(specs): imbalance_fade verdict (0021)`, the fields of spec 0020 AC-10: study, promotion id, evaluation date, verdict, every failed reason (code, value, threshold), walk forward Sharpe, holdout Sharpe against buy and hold, holdout return after costs, marked max drawdown, deflated Sharpe probability, PBO, holdout trades, trial count N and pool, `avg_hold_minutes`, and holdout gross and cost per trade. From the holdout `decisions`: exits by reason (`target`, `time_stop`, stop fill), unfilled entries (approved `enter` decisions minus positions opened), and the long and short net. A fail is recorded the same way and never removed.
- **AC-9** (close out): when the pre check failed or the study has a verdict, the AC-1 hash comparison runs again, the scope feature's Build it box is ticked, and on a `pass` (without a label) the paper follow up below is added. No revision of `imbalance_fade` runs in this spec.

## Decision

**Chosen option**: Option 1: the exact mirror of `book_imbalance`, behind a fresh day pre check.

One new pure rule that wraps `book_imbalance`'s signal and flips its side, run maker in and out in the replay lab, gated first by a 4 day pre check on unused days, then the standard study, holdout, and gate.

**Implementation skills**: `nautilus-trader` (`algoderiv/agent-skills`, `.claude/skills/nautilus-trader/`)

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Feature design

**Data model sketch**: no schema change. Runs, decisions, studies, and promotions use the existing lab and gate tables (specs 0008, 0015). The strategy gets its own `strategies` row and frozen `strategy_versions` hash through `ensure_strategy_version`, as every registered strategy does.

**State transitions**: the base's scalping position cycle (spec 0017), unchanged: flat → entry resting (post only, `entry_timeout_s` 10) → open with stop and resting target → closed by `target`, stop fill, or `time_stop` (5 minutes); an entry that does not fill in 10 seconds is cancelled and the rule is flat again.

**Interface surface** (existing CLI only):
| Command | Where | Key inputs | Key outputs | Key errors |
|---|---|---|---|---|
| `lab replay <precheck.toml> --from D --to D` | laptop (needs the laptop Postgres) | the AC-5 file, one day | run report JSON | 2 missing day manifest, 64 bad run file, 78 no `GIT_COMMIT` |
| `lab study studies/imbalance_fade.toml` | laptop | the AC-7 file | study batch, 4 grid runs | 64 plan changed under the same name |
| `lab holdout imbalance_fade` | laptop | study name | candidate and buy and hold runs | 64 grid incomplete |
| `scripts/push-bundles.sh <dirs>` | laptop to VPS | batch folders | loaded, skipped, refused counts | refused > 0 |
| `gate evaluate imbalance_fade` | VPS, `--no-deps` | study name | verdict, reasons, promotion id | run once only (a second run writes a duplicate row) |

**Value sourcing**:
| Action | Value | Source |
|---|---|---|
| fade signal | imbalance, depth, flow, persistence | `BookImbalanceState` (spec 0017 child 0004), unchanged |
| fade signal | side | the mirror of the parent signal's side (AC-2) |
| fade signal | stop and target prices | the parent signal's `inputs["mid"]` with `stop_bps` and `target_bps` from config (AC-2) |
| pre check | closed trades, net P&L, fees per day | each run's report JSON (`trade_count`, `net_pnl`, `fees_total`) |
| pre check | approved entries per day | `decisions` rows of the run with `intent` `enter`, `risk_verdict` `approved` (laptop Postgres), as spec 0017 task 19 did |
| pre check | pass bars 25 and 1.5 × | this spec AC-6 (25 gives about 1.5 × the 200 trade floor over 12 holdout days, 200 ÷ 12 = 16.7 being the bare minimum; 1.5 from `fee_hurdle_k`) |
| study and holdout | development and holdout days | `lab study`'s replay plan: every 1st before and from `holdout_start` 2025-10-19 (12 holdout days, 2025-11-01 to 2026-10-01, if run before 2026-11-02) |
| study | `max_spread_bps` 6.27 | spec 0017 AC-3 audit, never changed |
| results | unfilled entries | approved `enter` decisions minus the run's `positions` (AC-8) |
| verdict | every threshold and N | `gate/thresholds.toml` v1 and the gate's `kind+lab` family (kind `scalping`, lab `replay`) |

**Key invariants**:
- The gate thresholds and `thresholds.toml` are never changed by this spec.
- `book_imbalance`'s rule and node modules are untouched, so their frozen hash stays valid.
- The pre check days are never reused to pick settings; the study's grid is fixed above before the pre check runs.
- Every run counts as a trial in the family N, fails included.
- Maker in and maker target out; stops and time stops pay taker, as the base already does.

**Security model**: laptop only lab, demo only (`TRADING_ENV=live` exits 78). Nothing here places a live order or touches secrets beyond the existing Postgres file secret. The repo is public, so reports with run ids stay git ignored under `lab-reports/`.

**Configuration required**: none new.

**Critical test scenarios**:
- Mirror long: a parent long signal (bid heavy, positive flow, held 2 firings) gives a `SELL` with stop above and target below the mid by the configured bps, verifies **AC-2**
- Mirror short: the parent short gives a `BUY` with stop below and target above, verifies **AC-2**
- No signal: imbalance without matching flow, imbalance that does not persist, and an empty book side each give `None`, verifies **AC-2**
- Inputs passed through: `inputs` of the fade signal equal the parent signal's, verifies **AC-2**
- Node wiring: `ImbalanceFade` builds an `ImbalanceFadeState`, its `entry_reason` is `imbalance_fade_entry`, its config defaults equal `BookImbalanceConfig`'s, and the registry factory refuses a plain `BookImbalanceConfig`, verifies **AC-3**, **AC-4**
- Registry: `imbalance_fade` is registered as `scalping`, maker, with no candle flags, and `lab backtest` refuses it (no `candle_screen`), verifies **AC-4**
- Hash stability: the branch's node module hash list minus `imbalance_fade` matches the AC-1 baseline, verifies **AC-1**, **AC-9**
- Study file absent until the pre check passes (`test_scalp_studies.py` gains an `imbalance_fade.toml` absent check beside the `book_imbalance.toml` one, flipped to present once AC-7 commits it), verifies **AC-6**, **AC-7**

## Build plan

Tracer Bullet: the thinnest real thread is the rule, wired into the node and registry, replayed on the 4 pre check days. Only a pass grows it into the full study.

1. Branch from the agreed base, record the AC-1 hash baseline from that commit in `verify.md`, satisfies **AC-1**
2. `strategy/imbalance_fade.py` with its unit tests (mirror long, mirror short, no signal cases, inputs passed through), satisfies **AC-2**
3. `node/imbalance_fade.py`, the registry entry, node and registry tests; rebuild `kfs/lab` with `GIT_COMMIT`; commit `feat(lab): imbalance_fade rule, node, and registry (0021)`, satisfies **AC-3**, **AC-4**
4. Write the scratch pre check file and run the 4 days (about 2 hours), record the table and the AC-6 figures in `verify.md`, satisfies **AC-5**, **AC-6**
5. Checkpoint with the trader: report the pre check verdict. On a fail, go to step 8
6. Only on a pass: commit the study file (and flip its presence test), rebuild the image with `GIT_COMMIT`, run `lab study` (about 40 hours) and `lab holdout` (about 6 hours), push the bundles, run `gate evaluate` once (each VPS step confirmed), satisfies **AC-7**
7. Write the Results and `verify.md` run log, commit `docs(specs): imbalance_fade verdict (0021)`, satisfies **AC-8**
8. Close out: hash comparison again, scope tick, paper follow up only on a pass, satisfies **AC-9**

## Consequences

**Positive**:
- The cheapest honest test left: about 2 hours decides whether the 40 hour run is worth it.
- Maker in and out keeps the round trip at about 4 bps when the target fills, the lowest cost path the lab has.
- No new infrastructure, schema, or gate change; one small rule and one thin node.

**Negative / tradeoffs**:
- The idea came from looking at a failed result, so it is a data snooped trial. The family N and the deflated Sharpe charge for it, and the fresh pre check days limit the damage, but a pass still needs the holdout to be believed.
- Flipping the side does not flip the result: −0.95 gross does not become +0.95, because posting on the thin side changes which orders fill (thin queues fill faster, and often just as price runs through them). The pre check may well fail.
- 59 trades over 4 days (the `book_imbalance` sample) is thin evidence either way: at about 1 USD standard error a trade, the pre check is close to a coin flip even for a small real edge. Treat it as a long shot, not proof. 12 holdout days is a short year.
- A pass would still carry stop and time stop exits at taker cost.

**Neutral**:
- Joins the `scalping` `replay` family; its N grows by 4 if the study runs.
- `packages/core/AGENTS.md` should list `strategy/imbalance_fade.py` once it lands (a `/sync` item).

## Follow-up

- [ ] `/scope`: enroll feature 24 "Imbalance fade candidate" linking this spec.
- [ ] `/sync`: `packages/core/AGENTS.md` lists `strategy/imbalance_fade.py`.
- [ ] On a `pass` (without a label): `/scope` the feature that runs a registered strategy on the paper loop (same follow up as specs 0018 to 0020).
- [ ] If this fails too: `/architect` a multi coin version of the best candle rule (BTC plus ETH, the deferred ETH candles item) before any more single BTC rules.

## Results

Not run yet.

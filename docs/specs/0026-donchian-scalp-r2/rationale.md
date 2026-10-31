# 0026. donchian_scalp_r2: rationale

## Context

> ⚠️ Premise note: the evidence below says cheaper orders shrink r1's loss but do not remove it. The best maker variant with the volatility floor still lost about 0.36 to 0.71 ATR per trade in its bands, all in sample (2022 to 2023). The problem is mostly that r1 has no gross edge (gross −0.87 ATR per trade before costs), not that its costs are high. r2 is worth building only as a cheap test of the cost side, plus a reusable maker base. The engineer chose to run it on demo whatever its verdict.

r1 (spec 0025) trades a 5 minute Donchian breakout with an EMA 200 filter and a midline exit, at market. Its BTC screen failed. Over 5,002 lab trades, the round trip cost was 0.61 ATR at the median (0.83 mean), and the net loss was 1.70 ATR per trade. The engineer asked for better exits and take profits first. The exit study (below) showed that no exit, take profit, or time stop rescues it: every cell of the grid lost between 1.08 and 1.30 ATR.

The engineer then asked that every order be a limit order. Three forces shape that request:

- **Cost.** A taker round trip costs 10 bps in fees plus the spread and slippage. A maker round trip costs 4 bps. For a strategy whose bars are often only 5 to 10 bps tall, that difference is most of the result.
- **Safety.** The project rule is that anything touching orders fails closed. A protective stop that may not fill in a fast move breaks that rule, and so does a kill switch close that rests in the book.
- **Frozen code.** `BarRuleStrategy` (`node/bar_rule.py`) is in r1's version closure and every other bar strategy's. It submits market entries, treats only a complete fill as an entry, and cancels the stop before every exit. None of that can be edited.

Testing is limited. The candle lab fills a limit from synthetic quotes with no queue (`lab/candle_units.py`: L1 book, no queue position). The demo sandbox fills on quotes with no queue (`node/sandbox.py`). The replay lab can model a queue, but there are only about three weeks of recorded books.

## Options considered

### Option 1: subclass `BarRuleStrategy` again, as r1 did

Override `_decide_entry` to send a limit, and override the fill, refusal, and exit methods for partial fills and maker exits.

**Pros**:
- Least new code; the bar loop and safety paths stay shared.

**Cons**:
- The frozen base's `on_order_filled` acts only on a complete fill, and `_decide_exit` cancels the stop before every exit. r2 would override most of the order lifecycle through underscore members, which leaves a subclass that is a base in disguise and is hard to reason about.

### Option 2: a new maker base beside the frozen one (chosen)

`node/bar_rule_maker.py` copies the frozen base's bar loop and safety paths and adds the maker entry, the maker exit with fallback, and partial fills (adapted from `ScalpStrategy`). r2's node sits on it.

**Pros**:
- One clear lifecycle, testable on its own, and its own code hash. This is what the frozen base's own docstring asks for ("a change goes into a new module").
- Reusable for any later bar strategy that wants maker orders.

**Cons**:
- Duplicates the frozen base's code (about 700 lines), so a fix in one does not reach the other.

### Option 3: reuse `ScalpStrategy` (`node/scalp_base.py`)

It already has maker entries with a timeout, partial fills, and a maker target.

**Pros**:
- The maker paths are already written and tested.

**Cons**:
- Its entry rests at the touch, not at an ATR offset; its exits are a fixed price target and a time stop in minutes, with no bar driven rule exit. Adding the midline exit means overriding it, and it is frozen in four strategy closures.

### Option 4: keep r1's market orders and add only the volatility floor

**Pros**:
- Smallest change: one refusal in a node.

**Cons**:
- Gives up 0.5 to 0.75 ATR per trade of savings, and does not do what the engineer asked.

## Rationale

The cost force is real, so the cheaper orders are worth testing (Option 4 leaves them out). The frozen code force rules out editing the base. Of the two ways around it, Option 1 overrides nearly every order method through private members, and Option 3 is shaped around price targets, not bar rules. A new base (Option 2) costs some duplication, but it is the pattern the project already set for itself: frozen modules, and new modules for changes (basis: spec 0018 AC-7, the frozen base rule; spec 0025 AC-5).

The safety force decides which orders become limits. Entries and planned exits can wait, because the stop protects the position the whole time. The stop and every forced close cannot wait, so they stay market (basis: your AGENTS.md, fail closed for anything touching orders). That is also why the planned exit keeps the stop resting, unlike the frozen base, which cancels it first.

The maker exit was weighed on its own after the cross check: maker entry with market exits gave −1.27 ATR, and adding maker exits gave −1.08, so the exits are worth about 0.19 ATR per trade (about a third of r1's median cost) but carry most of the order race handling. The engineer kept them, since every planned order as a limit is the request, and the races are pinned down in AC-5 to AC-12 with a spike first.

The demo runs r2 whatever the screen says, by the engineer's choice. The cheaper path (promote only on a pass) was offered and declined: the point of the demo is to watch maker orders behave on a live feed (rejections, partial fills, fallbacks), which no screen shows, at the cost of ending r1's demo record.

The parameter picks follow the study. An offset of 0.5 ATR with a 3 bar wait gave the best maker result (−1.08 ATR overall, 75% filled). The floor of 10 bps skips the two worst bands, which hold most of the losses, while keeping about 58% of BTC signals. The engineer accepted each pick. The exit limit rests at the passive touch instead of the bar close the study used, because a post only limit at the close is often already at the bid or ask and would be rejected at once.

## Evidence

All figures are in ATR per trade at the signal, after costs, on r1's 5,002 BTC lab signals (2022-04 to 2023-10; r1 made no trades after 2023-10-15, so no out of sample half exists). Fees: taker 0.0005, maker 0.0002, slippage 2 bps on market fills. Scripts: the session scratchpad `exit_study.py`, `maker_study.py`, `maker_bands.py` (not committed; they read the r1 study bundle and the lab's 1 minute candles).

### Exit study (r1 as traded)

| Exit | Share | Gross | Median bars | Win rate |
|---|---|---|---|---|
| midline_exit | 75.8% | +0.05 | 12 | 29.3% |
| stop | 23.9% | −4.12 | 4 | 0% |
| time_stop | 0.2% | +31.5 | 60 | 100% |
| all | | −0.87 gross, −1.70 net | 9 | |

A triple barrier grid on the same entries (take profit 0.5 to 3 ATR or none, time stop 3 to 60 bars, same 3 ATR stop) lost between 1.08 and 1.30 ATR per trade in every cell. No exit fixes the entry.

### Maker study (entry and exit order types)

| Variant | Filled | Net per trade |
|---|---|---|
| taker in, taker out (r1) | 100% | −1.84 |
| taker in, maker out | 100% | −1.63 |
| maker at close, 1 bar | 80.5% | −1.36 |
| maker 0.25 ATR, 1 bar | 69.7% | −1.24 |
| maker 0.5 ATR, 1 bar | 55.3% | −1.15 |
| maker 1 ATR, 1 bar | 29.0% | −1.13 |
| maker at close, 3 bars, maker out | 90.4% | −1.26 |
| maker 0.25 ATR, 3 bars, maker out | 84.4% | −1.13 |
| **maker 0.5 ATR, 3 bars, maker out** | **74.9%** | **−1.08** |

The maker exit in the study was a limit at the exit bar's close, filled if the next 3 bars traded through it, else market.

### By volatility (ATR as share of price)

| ATR band | Signals | r1 (taker) | maker 0.5 ATR, 3 bars, maker out | filled |
|---|---|---|---|---|
| below 6 bps | 963 | −4.30 | −2.65 | 74% |
| 6 to 10 bps | 1,125 | −2.02 | −1.13 | 74% |
| 10 to 14 bps | 945 | −1.15 | −0.56 | 76% |
| 14 to 20 bps | 902 | −1.06 | −0.71 | 76% |
| 20 to 30 bps | 663 | −0.71 | −0.46 | 74% |
| above 30 bps | 404 | −0.66 | −0.36 | 75% |

Other filters tried on r1's trades (by quintile): breakout depth, bar range, EMA distance, and channel width showed no clear pattern. Volume below 0.48 of its 20 bar average was the worst quintile (−2.47). Hours 16 to 20 UTC were the least bad (−1.25), 04 to 12 UTC the worst (about −2.4). Only ATR as a share of price was monotone and has a structural reason (the fee is fixed in bps, the move is in ATR), so it is the one filter taken.

## References

**Project sources**:
- `AGENTS.md`: fail closed for anything touching orders; demo is the default
- Spec 0025 (`donchian_scalp_r1`): the rule, the seed, the spread check, the two coin wiring, AC-5 frozen modules
- Spec 0018 AC-7: the frozen base rule (`node/bar_rule.py`)
- Spec 0017 (`ScalpStrategy`): maker entry, entry timeout, partial fills, maker target, and its candle lab caveats (maker fills are optimistic, adverse selection seen in `maker_probe`)
- `node/sandbox.py` and `lab/candle_units.py`: how demo and the candle lab fill limit orders
- The r1 BTC study bundle (`lab-reports/01a18df4-...`) and the exit and maker studies above

**Practices & standards**:
- Fail closed protective orders: stops as stop market, forced closes at market
- Strangler rollout: the new variant runs beside the old, one switch to roll back
- Multiple testing: every screen is a trial in its family

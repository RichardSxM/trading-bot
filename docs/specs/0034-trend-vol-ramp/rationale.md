# 0034. Trend vol ramp: rationale

## Context

`trend_vol` (spec 0033) buys every coin's full target at once, at the daily rebalance or at any start or resume rebalance. On the VPS demo the trader was memory killed four times between 2026-11-20 and 2026-11-23. Each restart closes the in process sandbox book as abandoned and re-enters all 12 coins at full size an hour later, at whatever price that moment has. So entry prices are set by crash times, and one bad entry carries the whole position into the red.

You asked for a standard, proven entry method rather than more data mining, with the goal of limiting how deep a badly timed entry can go, not adding profit. Earlier research on this book (2026-11-17, spec 0033 precheck.md) closed take profits, pullback entries, and splitting the rebalance across the day (tranches), all worse or not robust. Scaling in over 3 days was the one soft lead. It had smaller drawdowns at 3 of 4 cut hours (50.2% vs 53.8% at 00 UTC) and a better H1 and H2 at 00 and 06, but it was slightly worse at 12 and 18, and part of the gain comes from a lower average gross (1.87 vs 2.07). The 8 vol stop changed nothing measurable (`scale3_nostop` matched `scale3_stop8`).

Constraints: `trend_vol`'s frozen hash backs a recorded gate fail. Everything that touches orders must fail closed. The VPS is short on memory and CPU, so per tick work is unwelcome. The live exchange keeps positions across restarts, while the demo sandbox does not.

## Options considered

### Option 1: Stateless grow cap plus a recorded stop (chosen)
Each daily rebalance adds at most a third of the target to the held position, and only the daily run may add. The stop is measured from the position's average entry, checked hourly, and rebuilt from `stop_vol` decision rows.
- Pros: no table or migration; restart safe on demo (flat → a third) and live (carries on); restarts never pick entry prices.
- Cons: differs from the tested rule (ramp by held size, not signal age; average entry, not first slice); demo is flat until the next 00:05 after any restart.

### Option 2: Signal age ramp, as tested
Size = full × min(1, days the signal has held this side / 3), from closes.
- Pros: matches the lab rule exactly; stateless.
- Cons: a restart in an old trend re-enters at full size at once, which is the problem you want fixed.

### Option 3: Stored ramp state
A Postgres table holding each coin's ramp day, entry price, and entry vol, reloaded on start.
- Pros: most faithful to the test (first slice entry price); full control.
- Cons: a migration, a write on every step, and drift between the table and the real position after a crash; still has to decide what a flat demo book means.

### Option 4: Edit `trend_vol` in place
- Pros: less code.
- Cons: changes the hash behind the recorded gate fail and the running demo, and there is no switch back.

## Rationale

The force that matters is restarts. Only a rule that reads the held position makes a crash cost a third instead of everything, on both demo and live, with no new state to drift. Letting only the daily run grow follows from the same force: start and resume rebalances happen at random times, which is exactly the entry timing you want gone. The 00:05 run is at the cut hour where the lab ramp helped.

You chose the 8 vol stop although the test showed no gain. It costs one hourly timer (no per tick load on a stretched VPS) and reuses decision rows for memory, so it is cheap. Keep it as a safety net and judge it on demo, not as an edge. Measuring from the average entry trades a small, known difference from the test for no saved price.

A new strategy keeps `trend_vol`'s record honest and makes rollback a one setting change. The runner up was Option 3, which is worth it only if the average entry stop proves too tight on demo.

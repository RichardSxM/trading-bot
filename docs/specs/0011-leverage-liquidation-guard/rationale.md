# 0011. Rationale: leverage and liquidation guard

The decision record behind [index.md](index.md). `/develop` builds from index.md and can skip this file.

## Context

The trader runs one placeholder strategy on BTC perpetuals in demo, with live trading planned for feature 19 under 1000 EUR and at most 2x leverage. Spec 0006 shipped a pure `check_entry` with two fixed caps: the order notional, and open notional over equity. Every entry is a fixed 100 USD, whatever its stop. Spec 0010 added the kill switch and the loss limits on top. Slice 4 of the scope makes this feature, the loss limits, and restart safety hard requirements before any live money.

Four gaps remain.
- **Size ignores risk.** A fixed notional means a trade with a 1 % stop risks more than three times one with a 0.3 % stop. The loss limits (20 USD a day) then trip after a different number of trades depending on which strategy ran. The article carried into the scope recommends sizing from the stop instead, with fees counted, since on a tight stop the fees can be a large share of the loss.
- **Nothing looks at liquidation.** Kraken's multi collateral perpetuals use cross margin: the whole account backs a position, and the exchange force closes it when equity falls to the maintenance margin. At 2x and a sub percent stop this is far away, but nothing stops a config change (higher leverage, a wider stop) from putting the liquidation inside the stop, where the exchange closes the position before the stop can.
- **Exposure across instruments.** ETH is planned. The leverage cap measures total open notional over equity, so total exposure grows with equity by itself; there is no fixed USD ceiling.
- **Funding is missing.** Perpetuals pay or earn funding every hour. The dashboard labels every figure "before funding", the loss limits ignore it, and spec 0002 left open how demo funding would reach `funding_payments`. The NautilusTrader sandbox applies no funding, so the trader must compute it. The replay lab already does (spec 0008 AC-9, spec 0009 AC-14), keeping `realized_pnl` before funding and the funding in `funding_total`.

Two constraints shape every answer. Anything touching orders fails closed. And the risk logic must stay pure in `packages/core` with no I/O, so the same check runs in the trader and the lab.

Without this feature, size is arbitrary, liquidation is unchecked, and P&L overstates any strategy that holds across the hour.

## Options considered

### Option 1: Separate guard stage after `check_entry`

Leave `check_entry` as it is, and add a second pure function, `guard_entry`, that sizes from risk, checks liquidation, and adds funding, called after `check_entry` approves. Funding booking goes in a new `FundingActor`.

**Pros**:
- `check_entry` and its tests stay untouched.
- Each stage is small and easy to read on its own.

**Cons**:
- Two calls that must agree on the same inputs, in every strategy; a new strategy can forget the second.
- The scope note for this feature says to extend `check_entry`, not fork it.
- A `FundingActor` needs each position's row id, which only the `RecordingActor` holds, so it would need a lookup or a duplicate map.

### Option 2: Extend `check_entry` into the single guard; book funding in the recording actor (chosen)

`check_entry` sizes, clips to the caps, checks liquidation and the funding term, and returns the quantity with the verdict and its figures. Pure liquidation and sizing helpers live in `trading_core.risk`. The `RecordingActor` books funding at each hourly boundary and bumps `funding_total` in the same transaction as the insert.

**Pros**:
- One call, one refusal order, one place to read; every strategy gets the whole guard.
- The recording actor already has the row ids, the write queue, and timers.
- Reuses the lab's funding rule, so paper and replay book funding the same way.

**Cons**:
- `EntryRequest` grows to about a dozen fields, and every strategy must supply them.
- The recording actor takes on a second job (booking), so it grows.

### Option 3: Let the exchange model decide

Configure the NautilusTrader sandbox and risk engine (leverage per instrument, margin model, `max_notional_per_order`) to reject oversized orders, and read liquidation from the venue.

**Pros**:
- Less of our own code; the engine already rejects orders over its limits.

**Cons**:
- The sandbox does not liquidate or charge funding, so demo would never show either.
- A rejection from the engine arrives after submit, as an order event, not as a recorded decision with figures.
- Sizing from risk is still ours to write.

## Rationale

The main risk here is a gap between what a strategy thinks it may do and what it is allowed to do. One function deciding size and permission together closes that gap: a strategy cannot submit a size the guard did not compute. That is why Option 2 beats Option 1, and it follows the spec 0006 instruction to extend `check_entry`. Option 3 fails the demo test outright, since the sandbox never liquidates and never charges funding, so nothing would be proven before live.

The specific calls follow from the forces in Context.
- **Sizing.** You chose risk sizing with the caps on top and the smaller winning, and 0.5 % per trade. That gives four stopped trades before the 20 USD daily limit, with room for slippage.
- **Liquidation.** You chose the cross margin formula, because Kraken margins the whole account and an isolated formula would be wrong there. Both distance rules apply: the percent floor catches leverage set too high, and the stop multiple catches a stop set too wide.
- **Funding before the trade.** It counts only when the hold crosses a boundary, and a stale rate refuses only then. That keeps the placeholder, which never crosses the hour, from being blocked by a funding feed it does not need.
- **Funding after the trade.** It stays in its own column, net = `realized_pnl + funding_total`, because the lab already works that way. Folding it into `realized_pnl` would be overwritten by the next position upsert from NautilusTrader.
- **Exposure.** A fixed USD exposure cap sits on top of leverage, so a growing account does not raise its own ceiling.
- **Live funding.** Deferred to feature 19, because nothing trades live before then and the Kraken keys exist only on the VPS.
- **Fee source (corrected 2026-10-15).** The first version used `instrument.taker_fee`, assuming the sandbox's fee reached the node cache. It does not: Kraken publishes 0, and `FeeSandboxExecutionClient` patches only its own exchange's copy. The VPS verify run caught entries sized without fees (spec 0011 verify step 2). The fee is now config: it is a property of the venue and the account, not of the instrument, so whoever builds the strategy config supplies it. The live value depends on the account's fee tier, which is a feature 19 decision, and the trader refuses live until it is made.

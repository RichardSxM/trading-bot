# 0025 rationale: donchian_scalp_r1

## Context

> ⚠️ Premise note: every scalper so far failed on the fee hurdle (spec 0017's candidates, `imbalance_fade` in spec 0021: gross P&L per trade below the cost per trade). A round trip costs about 14 bps of notional (taker fee 0.05% each side plus 2 bps slippage each side, spec 0019 *Cost arithmetic*). The midline exit takes profit about half a channel width from the far side, which on 5 minute BTC bars can be only a few tens of bps, so it may lower gross per trade, not raise it. The EMA filter cuts trades too. That is why the lab screens compare gross per trade against cost per trade, not only net P&L. If r1's gross per trade is below its cost, demo results will only confirm the same failure more slowly.

`donchian_scalp` runs on the VPS demo since 2026-10-29 (PR #108). It is the `donchian_4h_pair` grid point (20 bar channel, 3 ATR stop, 60 bar time stop) moved to 5 minute bars for quicker trades. It has no lab study and no gate pass. It takes every breakout in both directions, exits on the opposite 10 bar channel, and enters with no check on the spread.

On 2026-10-30 you shared a Donchian M1 scalping blueprint. Three of its ideas apply to this strategy: a 200 EMA trend filter, an exit at the channel midline, and a spread filter. Two do not: M1 bars and market orders make the fee hurdle worse, and risk sizing, loss limits, cooldown, and an ATR stop already exist.

Forces: the `BarRuleStrategy` base and `DonchianRule` are frozen after their first study (a change goes into a new module), and so is `donchian_scalp`'s node, whose code hash identifies its promoted version. The strategy builds its own 5 minute bars from live trades, so any indicator needs its full window of bars after every start. An EMA 200 needs about 16.7 hours, and you deploy several times a week. The candle lab fakes the quote at `slippage_bps` either side of the price (about 4 bps wide), while 0.1 ATR on 5 minute BTC bars is around 2 bps. The lab has no real spread to judge a spread filter with.

## Options considered

### Option 1: New variant beside the old one (chosen)

A new pure rule `DonchianMidlineRule` (composition over `DonchianRule` plus `Ema`) and a new node `DonchianScalpR1` on the frozen base. The spread check overrides the node's entry decision, and a startup seed fills the indicators.

**Pros**:
- Nothing frozen moves. `donchian_scalp` stays promotable and is one env line away.
- The lab can compare old and new on the same data.
- Small: two modules, one override, one seed path.

**Cons**:
- The subclass leans on the base's underscore members.
- Some duplicated wiring in `paper_strategy.py` (a third donchian config builder).

### Option 2: Edit donchian_scalp in place

Add the filters and exit to `donchian_scalp` directly.

**Pros**:
- No new strategy name, no new wiring.

**Cons**:
- Breaks the frozen module rule and changes its promoted version's hash (deploy exits 4 until a new promote).
- Loses the baseline to compare against, in the lab and on demo.

### Option 3: A `bar_rule_v2` base with reusable filters

A new base with hooks for entry filters (spread, trend) and a seeded warmup, then r1 on top of it.

**Pros**:
- Clean hooks, no subclassing of private members, reusable by later candidates.

**Cons**:
- Copies or reworks about 700 lines of order lifecycle that is proven on demo and in specs 0018 to 0023, for one strategy.
- A bigger change than the idea under test. Better done when a second strategy needs the same hooks.

## Rationale

Option 1 tests the blueprint ideas with the least new surface while keeping the frozen version rule, which is what lets the gate and the deploy trust a code hash. Option 2 would erase the only baseline you have. Option 3 is right only once a second strategy needs the same filter hooks; today it would be speculative work on the riskiest code (the order lifecycle).

Sub decisions, each chosen in the design conversation:

- **Validation: two one point candle screens, then demo whatever the verdict.** A full gate study (grid plus holdout) is slower and adds more trials for a demo only strategy. Going straight to demo gives no baseline. The screens give an honest before and after at the cost of 2 family trials.
- **Warmup: seed from Kraken's public 5 minute candles.** Accepting a 17 hour warmup after every deploy wastes most demo days in a deploy heavy week. A shorter EMA changes the idea under test. Reading the recorder's catalog would couple the trader to the recorder's disk and hourly compaction. Nautilus `request_bars` through the Kraken adapter is unproven for this pin. The charts API is the same source the lab already fetches from, public, and needs no key. On failure it falls back to the warmup that would happen anyway, so it fails safe.
- **Trend filter: close against the EMA, no slope.** Matches the blueprint and adds one setting, not two.
- **Midline exit: rolling, replacing the 10 bar channel exit.** It isolates the blueprint's exit. Keeping both mostly duplicates the midline (the midline is almost always crossed first). A midline frozen at entry would be a fixed target in disguise.
- **Spread filter: `spread > 0.1 × ATR`, off in the lab.** It scales with volatility, unlike a fixed bps cap. In the lab it would block nearly everything against the synthetic spread, so the lab studies leave it off and demo judges it.
- **Spread skip: a refused decision row, no alert.** It stays countable in the journal without paging you.
- **Spread check in the node, not an `EntryBlocks` reason.** A block reason is global, shown on the dashboard, and raised from timers. A spread is a fact of one decision at one bar close, so it belongs in that decision's record.
- **Seed join at the first live bar, which is thrown away.** The first live bar covers only part of its interval, so its high and low would distort the ATR and the channel and it could be a deciding bar. Ignoring it costs one bar. The join is checked at that bar, not at `on_start`, because Kraken connect and reconcile can take a while and that wait should not make a good seed look stale. One missing bar between seed and live is normal on a slow start, and its effect on a 200 bar EMA and a 20 bar channel is small. More than that means the seed is out of date, and the honest fallback is the cold warmup.
- **Spread check after the gate and blocks.** A halted or paused trader with a wide spread would otherwise record `spread_wide` and hide the real reason in the journal.
- **Cold start time stop risk accepted, not fixed.** Running the time stop while the rule warms up would mean overriding the base's `on_bar` exit logic, the riskiest code. The case needs a seed failure and a restart with an open position together, and the ATR stop still protects that position.
- **Build order: lab screens before the spread filter and the seed.** The screens need only the rule. Running them early answers the fee hurdle question before more code is built. Demo follows whatever the verdict, so nothing is skipped.

## Amendment: two coins (2026-10-30)

*Cross check (Sonnet, read only) found 14 gaps; all fixed as recommended.* The main one: each strategy's frozen stale check adds and clears the shared `STALE_FEED` block for its own coin, so a fresh BTC would clear a stale PEPE's block every second. A trader side wrapper per coin fixes this without touching the frozen `stale.py`. The others: PEPE's average entry price rounded to cents (0.00), the hardcoded `BTC` unit on the card, the card's mixed and `unknown` states, `sides` per row, funding and liquidation details for two coins, a start check for a coin missing from the instrument cache, PEPE sizing tests, the AC-25 outcome, a PEPE flatten test, the demo rollback wording, `order_id_tag` kept out of the version, and the shared trade cap as a risk.

### Context

After both screens failed, you still wanted r1 on the demo, and also on PEPE. The trader was built around one coin: `BTC_PERP` was fixed in `trader/main.py` for the paper strategy, the recording actor, the snapshots, the per order cap, the context feed, and the seed URL. Much of the rest already worked for many coins: the data client loads every instrument, the sandbox sets fees on every instrument, the kill switch attaches every strategy, the loss limits and the guard's exposure sum every position, and every row that needs a coin has `instrument_id`. The frozen strategy code (`node/bar_rule.py` and its closure) takes one `instrument_id` per instance, which fits one instance per coin.

### Options considered

- **One trader, one strategy per coin, shared account (chosen by you).** Pros: one stack, one paper account, one run, and both coins on one dashboard. The frozen strategy code is untouched, because each instance is still single coin. Cons: the trader side modules (recording actor, snapshots) change, and so does the hash check rule. The coins share blocks and budgets.
- **A second trader service for PEPE only.** Pros: no trader code change beyond the seed URL, and per coin blocks come for free. Cons: a second Compose service, its own Redis keys, paper account, run, and dashboard view; two sandboxes that do not see each other's exposure; and more on the VPS to operate.
- **One version per coin.** Pros: clean per coin promotions and stats. Cons: two promotes and two run ids on one shared account, which the run and journal model does not expect.
- **Coin list outside the version.** Pros: simplest. Cons: the record would no longer say which coins a paper run traded, which breaks the rule that a version names what ran.

### Rationale

- **The coin list sits in r1's version only.** Other strategies stay single coin, so their versions and hashes do not move. A change to the set is visible in the promotions, like any other config change.
- **The full 0.5% per coin, not a split.** Splitting would need a per coin risk value through the frozen sizing, or a trader side override of it. The account wide exposure cap and the loss limits already bound the combined risk, and each coin's trades read like a standalone run.
- **Shared blocks.** Per coin blocks would mean one `EntryBlocks` per coin, and the dashboard and alerts would have to say which coin is blocked. Failing closed on both is the safer side for a first two coin run. It stays a follow up if PEPE's stale quotes stop BTC too often.
- **One `positions` snapshot with a list, not a key per coin.** Snapshot names cannot hold a colon (`snapshot_key`), and one key keeps one age check for the whole card. Every row in it keeps today's per position logic.
- **One recording actor for all coins.** Order, position, and account events are subscribed by topic, so a second actor would record every event twice. Its component id is also fixed.
- **A PEPE screen after the guard fix.** The off tick check was silently off for PEPE (spec 0022 review). A screen on candles the check never looked at would be weaker evidence. One hundredth of the tick leaves every coin with a tick of `1e-6` or more exactly as today.
- **The same 0.1 spread value for PEPE, measured once there is data.** Without recorded PEPE quotes, any other value would be a guess. AC-25 sets the rule for amending it.
- **Both coins in one deploy.** The version holds the coin set, so going BTC first and adding PEPE later would mean two promotes for no extra safety on demo.

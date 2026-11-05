# 0028 rationale: go live with small capital

## Context

> ⚠️ Premise note: no strategy has passed the promotion gate. Every scalping, hour level, swing, trend and market context candidate failed, and r2 on demo is losing. So "go live" cannot mean trading a strategy today. This spec builds and proves the live path so that a version that does pass can go live safely and quickly, and it moves real money only in the canary.

The trader has run only against the demo sandbox since Kraken closed its futures demo on 14 July 2026. Many pieces are ready: `TRADING_ENV=live`, the env guard, the key check (spec 0003), the restart reconcile and live drill (spec 0023), the kill switch and loss limits (spec 0010), the leverage guard (spec 0011), and a `paper → live` gate step (spec 0015 AC-16). But live is still refused in several places, each pointing at feature 19: the env guard allowlist, `check_version`, and the guard's fee source.

Four forces shape the design:
- **Real money with nothing to practise on.** The only real exchange is your own account, so the first orders must be tiny and checked step by step.
- **The gate's exact match rule.** A promotion names one frozen hash, which covers the code plus every `risk_*` setting. Live risk settings that differ from demo's make a different version.
- **A tiny account.** 100 USD with 0.5% risk means about 0.50 USD per trade, close to Kraken's minimum sizes on some coins and small next to fees.
- **One 4 GB VPS** running the recorder, Postgres, Redis, the demo trader and the dashboard.

Carried over from earlier specs: the dead man's switch (0001, 0006), the stop trigger and partial entries (0006), fees and funding from the account log (0006, 0011), canary latency (0008), live key checks (0003), and the 2026-10-17 risk profile proposal (0.25% per trade, 1% daily, 3% drawdown, a 3 loss streak stop). That proposal was weighed one rule at a time, and you set different values.

## Options considered

### Option 1: Build the machinery, unlock live only with a gate pass and a sign off (chosen)

Build live mode, the canary, the ledger, the live limits and the sign off now. Strategy trading stays locked until a version earns the paper to live pass at the exact live config.

**Pros**: no money moves on a losing strategy; the plumbing is proven by cheap canary orders; when a strategy passes, going live is a sign off, not a build.
**Cons**: real work with no live trading to show for it until a strategy passes; demo must trade at live sizes.

### Option 2: Canary only, strategy trading in a later spec

Prove the Kraken path with the canary, then stop. Leave the sign off, limits and ledger for later.

**Pros**: smallest build.
**Cons**: the design of the limits and sign off is deferred to the moment a strategy passes, which is when you most want to move fast.

### Option 3: Live r2 as a paid experiment

Manually promote r2 to live with tiny capital, to learn real fills.

**Pros**: real fill data sooner.
**Cons**: the backtests and the demo week say r2 loses money, and the gate deliberately refuses a manual only version on live (spec 0015). It spends money to confirm a known loss.

## Rationale

Option 1 fits the forces. The premise note rules out trading a strategy now, and Option 2 only postpones the design of limits that are cheap to decide today. Option 3 conflicts with the gate's own rule.

Key sub decisions, with the runner up:
- **Demo trades the exact live config** (runner up: match on code hash only). This keeps the gate's exact frozen hash rule intact, and it tests the 100 USD minimum sizes on paper first. It costs a fresh 30 day paper record, which is free today because nothing passes anyway.
- **Side by side stacks** (runner up: live replaces demo). The live trader and dashboard are extra services in the existing `compose.live.yaml`, sharing Postgres, Redis and the recorder. Every row and key already carries `env`. Demo keeps producing the comparison data the cost drift check needs.
- **The paper to live checks stay at spec 0015's thresholds** (30 days, 100 scalping trades, fill rate and slippage tolerance). They are stricter than the 14 days and 30 trades first suggested in the interview, and weakening an accepted gate needs its own decision.
- **The dead man's switch is armed only while an entry rests.** Kraken's switch cancels every order, stops included. Always armed would strip the stop from an open position at the exact moment the bot is dead. Armed only while a resting entry could open a position, it protects against the real risk (a fill with no bot) and never against the stop.
- **Mark price stops** (runner up: last price). Kraken liquidates on mark, and mark ignores a single stray trade on a thin alt book. The lab difference is small and watched.
- **Kraken's account log is the source of truth** for fees and funding, stored raw in its own table. The fills keep the trader's own figure, so a mismatch stays visible and halts.
- **Own thin REST client** (runner up: the adapter's internal Rust client). NautilusTrader 1.231.0's Kraken adapter exposes no switch, fee schedule or account log call. Three signed calls in `urllib`, as the seed fetch already does, beat depending on undocumented internals or adding an SDK that touches the key.
- **Review halts clear only from a terminal with a reason.** Drawdown, ledger and cost drift halts mean something is wrong, not just a bad day. A phone tap should not clear them. The control CLI already exists, so this is a rule, not a new tool.
- **The sign off confirms a gate pass** (runner up: a separate sign off table). Spec 0015's `no_manual_live` check forbade any manual live row, to stop a manual only route to live. A `confirms_promotion_id` link keeps that intent (a manual live row must point at a gate pass for the same hash) while keeping every promotion in one table that `gate versions` already shows.
- **Total loss counts from the sign off time, outside the hash.** `trader.version` hashes every `risk_*` field of `TraderSettings`, `RISK_TOTAL_LOSS_SINCE` included. Requiring it to equal the sign off date would make the live hash differ from demo's. The later of the setting and the sign off time gives the same protection with no hash change.
- **USD only wallet.** Equity is Nautilus' `balance_total`. With other collateral, Kraken values it with haircuts that the trader cannot reproduce, so the start refuses it instead.
- **Keep the switch despite the gap** (runner up: taker only entries in live). The switch cancels other coins' stops if the bot dies while an entry rests. Compose restarts a crashed trader within seconds, and spec 0023's reconcile replaces missing stops, so the gap is mainly a long VPS or network outage. Taker only entries would close it, but they rule out maker strategies like r2 for good.
- **Mark price stops wait on a spike.** It's unproven that the Nautilus sandbox fires mark triggered stops. A demo whose stops never fire would be worse than a small lab to live difference, so task 3 proves it first.
- **Drill pass by typed date.** Spec 0023 records its live drill only in `verify.md`. Typing the date at sign off is honest and costs nothing. A stored drill result would be a change to spec 0023.

Your risk values versus the proposal: 0.5% per trade (same as demo, so live compares one to one with paper), a 2% daily stop (about 4 full losses at 0.5%), a 10% drawdown halt (about 20 full losses, a real signal on a tiny account), no streak rule (the gate never tested one), a 50 USD final stop, and the demo's caps scaled to 100 USD (25 per order, 50 total, so about two trades at once).

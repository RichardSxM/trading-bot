"""covers: spec 0025 AC-4 (config checks), AC-6 (the spread check), AC-7 and AC-10 (the seed joins the live bars),
AC-12 (decision inputs).

Runs ``DonchianScalpR1`` in a BacktestEngine on synthetic trades and quotes for a PF_XBTUSD shaped instrument
(tick 1), on 5 minute bars, with short windows (entry 2, EMA 2, ATR 2) so a few bars make the rule ready. The
spread tests swap in a scripted rule. Needs the ``node`` extra.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import cache
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity

from trading_core.control.pause import PauseState
from trading_core.node.bar_rule import BarRuleConfig, BarRuleStrategy
from trading_core.node.donchian_scalp_r1 import DonchianScalpR1, DonchianScalpR1Config
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import ControlSource, Intent, OrderSide, RiskVerdict
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side
from trading_core.strategy.blocks import BlockReason, EntryBlocks
from trading_core.strategy.donchian_midline import DonchianMidlineRule

D = Decimal
START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
FIVE_MIN_NS = 300_000_000_000
INSTRUMENT_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
INSTRUMENT = CryptoPerpetual(
    instrument_id=INSTRUMENT_ID,
    raw_symbol=Symbol("PF_XBTUSD"),
    base_currency=BTC,
    quote_currency=USD,
    settlement_currency=USD,
    is_inverse=False,
    price_precision=0,
    size_precision=4,
    price_increment=Price.from_str("1"),
    size_increment=Quantity.from_str("0.0001"),
    margin_init=Decimal("0.02"),
    margin_maint=Decimal("0.01"),
    maker_fee=Decimal("0.0002"),
    taker_fee=Decimal("0.0005"),
    ts_event=0,
    ts_init=0,
)


def _ns(at):
    return int(at.timestamp() * 1_000_000_000)


def rising(t):
    return 85000 + int(t * 10)


def flat(t):
    return 85000


def _data(prices, minutes, every_s=10):
    """A trade and a quote (1 either side of the mid) every ``every_s`` seconds; ``prices(t)`` at t minutes."""
    data, at = [], START
    for step in range(int(minutes * 60 / every_s)):
        mid, ts = prices(step * every_s / 60), _ns(at)
        trade_id = TradeId(f"T{step}")
        data.append(
            TradeTick(INSTRUMENT_ID, Price(mid, 0), Quantity.from_str("0.0100"), AggressorSide.BUYER, trade_id, ts, ts)
        )
        data.append(
            QuoteTick(
                instrument_id=INSTRUMENT_ID,
                bid_price=Price(mid - 1, 0),
                ask_price=Price(mid + 1, 0),
                bid_size=Quantity.from_str("5.0000"),
                ask_size=Quantity.from_str("5.0000"),
                ts_event=ts,
                ts_init=ts,
            )
        )
        at += timedelta(seconds=every_s)
    return data


def _fields(**overrides):
    return {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": str(INSTRUMENT_ID),
        "bar_spec": "5-MINUTE-LAST",
        "atr_period": 2,
        "time_stop_bars": 2,  # a 10 minute hold crosses no funding time (no market context here)
        "use_market_context": False,
        "max_order_notional_usd": D(50000),
        "max_leverage": D(5),
        "max_total_exposure_usd": D(50000),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
        "stale_quote_s": D(600),
    } | overrides


def _config(**overrides):
    values: dict[str, Any] = _fields(entry_bars=2, stop_atr=D(1), ema_period=2) | overrides
    return DonchianScalpR1Config(**values)


class Script:
    """A scripted ``BarRule``: ready from bar 3; at bar ``n`` in ``entries`` a long 30 below the close."""

    def __init__(self, entries=()):
        self.entries = set(entries)
        self.n = 0
        self.close = D(0)

    def update(self, bar, atr):
        self.n += 1
        self.close = bar.close

    def ready(self):
        return self.n >= 3

    def entry(self):
        if self.n not in self.entries:
            return None
        return EnterSignal(Side.LONG, self.close - 30, "script_long")

    def consumed(self):
        self.entries.discard(self.n)

    def exit(self, trade):
        return None

    def on_flat(self):
        pass

    def values(self):
        return {"n": self.n}


class _Scripted(DonchianScalpR1):
    """r1 with a scripted rule in place of its own, so the spread check is tested alone."""

    def __init__(self, config, gate, blocks, record, rule):
        super().__init__(config, gate, blocks, record)
        self.rule = rule


class _BaseScripted(BarRuleStrategy):
    def __init__(self, config, gate, blocks, record, rule):
        super().__init__(BarRuleConfig(**_fields()), gate, blocks, record, rule)


class _Counting(DonchianScalpR1):
    """r1 that notes the close time of every bar its rule is fed."""

    def __init__(self, config, gate, blocks, record, seed=()):
        super().__init__(config, gate, blocks, record, seed)
        self.fed: list[int] = []
        update = self.rule.update

        def counted(bar, atr):
            self.fed.append(bar.ts_ns)
            update(bar, atr)

        self.rule.update = counted  # type: ignore[method-assign]


def _engine(prices, minutes):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(10000, USD)],
        base_currency=USD,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_data(prices, minutes))
    return engine


def _run(build, prices=rising, minutes=40, gate=None, blocks=None):
    engine = _engine(prices, minutes)
    records: list[WriteItem] = []
    strategy = build(gate or PauseGate(), blocks or EntryBlocks(), records.append)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _decisions(rows):
    return [r for r in rows if isinstance(r, DecisionRow)]


def _scripted(entries, **overrides):
    return lambda gate, blocks, record: _Scripted(_config(**overrides), gate, blocks, record, Script(entries))


@cache
def _live_closes():
    """The close times of the live bars the synthetic data makes, read from a run with no seed."""
    _, strategy, _ = _run(lambda g, b, r: _Counting(_config(), g, b, r), prices=flat)
    return tuple(strategy.fed)


def _seed(last_close_ns, count=6, close=85000):
    first = last_close_ns - (count - 1) * FIVE_MIN_NS
    return [
        BarInput(first + i * FIVE_MIN_NS, D(close), D(close + 1), D(close - 1), D(close), D(1)) for i in range(count)
    ]


def _codes(caplog):
    return [(f.get("code"), f.get("reason")) for r in caplog.records if (f := getattr(r, "fields", {})).get("code")]


# Config (AC-4) ------------------------------------------------------------------------------


def test_the_defaults_are_the_demo_settings_with_the_filter_off():
    config = DonchianScalpR1Config(**{k: v for k, v in _fields().items() if k != "time_stop_bars"})
    assert (config.bar_spec, config.entry_bars, config.stop_atr, config.time_stop_bars) == (
        "5-MINUTE-LAST",
        20,
        D(3),
        60,
    )
    assert (config.atr_period, config.atr_floor_mult, config.ema_period, config.spread_max_atr) == (2, D(1), 200, None)


def test_the_node_builds_the_midline_rule_from_the_config():
    strategy = DonchianScalpR1(_config(entry_bars=20, ema_period=200), PauseGate(), EntryBlocks(), lambda item: None)
    assert str(strategy.bar_type) == "PF_XBTUSD.KRAKEN-5-MINUTE-LAST-INTERNAL"
    assert isinstance(strategy.rule, DonchianMidlineRule)
    assert (strategy.rule.inner.entry_bars, strategy.rule.ema.n) == (20, 200)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"ema_period": 1}, "ema_period"),
        ({"spread_max_atr": D(0)}, "spread_max_atr"),
        ({"spread_max_atr": D("-0.1")}, "spread_max_atr"),
        ({"bar_spec": "1-MINUTE-LAST"}, "bar_spec"),
        ({"entry_bars": 1}, "entry_bars"),
        ({"stop_atr": D("0.5")}, "stop_atr"),
    ],
)
def test_a_bad_setting_is_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        _config(**change)


# Spread check (AC-6) ------------------------------------------------------------------------


def test_a_wide_spread_records_spread_wide_and_sends_no_order(caplog):
    with caplog.at_level(logging.INFO):
        engine, _, rows = _run(_scripted({5}, spread_max_atr=D("0.01")))

    [refused] = _decisions(rows)
    assert (refused.intent, refused.side, refused.reason) == (Intent.ENTER, OrderSide.BUY, "script_long")
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "spread_wide")
    inputs = refused.inputs
    assert {
        "bar_close",
        "atr",
        "n",
        "structural_stop",
        "bid",
        "ask",
        "spread",
        "spread_limit",
        "mid",
        "quote_age_ms",
    } == set(inputs)
    assert D(str(inputs["spread"])) == 2
    assert D(str(inputs["spread_limit"])) == D("0.01") * D(str(inputs["atr"]))
    assert D(str(inputs["structural_stop"])) == D(str(inputs["bar_close"])) - 30
    assert engine.cache.orders() == []
    assert ("entry_skipped_spread", None) in _codes(caplog)


def test_a_narrow_spread_enters_as_the_base_does():
    engine, _, rows = _run(_scripted({5}, spread_max_atr=D("0.1")))

    [enter] = [d for d in _decisions(rows) if d.intent == Intent.ENTER]
    assert (enter.risk_verdict, enter.risk_reason) == (RiskVerdict.APPROVED, None)
    assert D(str(enter.inputs["atr"])) * D("0.1") >= 2
    assert engine.cache.orders()


def test_with_the_filter_off_the_decisions_match_the_base_bar_for_bar():
    def key(rows):
        return [(d.intent, d.reason, d.risk_verdict, d.risk_reason, d.decided_ns) for d in _decisions(rows)]

    _, _, r1 = _run(_scripted({4, 9}))
    _, _, base = _run(lambda g, b, r: _BaseScripted(None, g, b, r, Script({4, 9})))
    assert key(r1) == key(base)
    assert key(r1)


def test_a_paused_gate_with_a_wide_spread_records_the_pause_not_spread_wide():
    gate = PauseGate()
    gate.pause(PauseState(ControlSource.DEPLOY, START, uuid.uuid4()))

    _, _, rows = _run(_scripted({5}, spread_max_atr=D("0.01")), gate=gate)

    [refused] = _decisions(rows)
    assert refused.risk_verdict == RiskVerdict.REJECTED
    assert refused.risk_reason == "paused"


def test_a_blocked_trader_with_a_wide_spread_records_the_block_not_spread_wide():
    blocks = EntryBlocks()
    blocks.add(BlockReason.REGIME)

    _, _, rows = _run(_scripted({5}, spread_max_atr=D("0.01")), blocks=blocks)

    [refused] = _decisions(rows)
    assert refused.risk_verdict == RiskVerdict.REJECTED
    assert refused.risk_reason not in (None, "spread_wide")


@pytest.mark.parametrize(("atr", "wide"), [(D(20), False), (D(10), True)])
def test_a_spread_equal_to_the_limit_passes_and_one_tick_more_is_wide(atr, wide, monkeypatch):
    # After a run the cache holds the last quote, 1 either side of the mid: a spread of 2.
    _, strategy, _ = _run(_scripted((), spread_max_atr=D("0.1")))
    passed: list[Decimal] = []
    written: list[WriteItem] = []
    monkeypatch.setattr(BarRuleStrategy, "_decide_entry", lambda self, bar, atr, signal: passed.append(atr))
    strategy._gate = PauseGate()  # the stop at the run's end paused the run's gate
    strategy._blocks = EntryBlocks()
    strategy._record = written.append
    bar = BarInput(0, D(85000), D(85001), D(84999), D(85000), D(1))

    strategy._decide_entry(bar, atr, EnterSignal(Side.LONG, D(84000), "script_long"))

    refused = [d for d in _decisions([w.row for w in written]) if d.risk_reason == "spread_wide"]
    assert (len(refused), passed) == ((1, []) if wide else (0, [atr]))


# Warmup seed (AC-7, AC-10) ------------------------------------------------------------------


def _with_seed(seed):
    return lambda g, b, r: _Counting(_config(), g, b, r, seed)


@pytest.mark.parametrize("gap", [FIVE_MIN_NS, 2 * FIVE_MIN_NS])
def test_a_seed_up_to_two_intervals_before_the_first_live_bar_is_fed_and_that_bar_ignored(gap, caplog):
    live = _live_closes()
    seed = _seed(live[0] - gap)

    with caplog.at_level(logging.INFO):
        _, strategy, rows = _run(_with_seed(seed), prices=flat)

    assert strategy.fed == [b.ts_ns for b in seed] + list(live[1:])
    assert strategy.rule.ready()
    assert ("warmup_seed_fed", None) in _codes(caplog)
    assert _decisions(rows) == []  # flat live bars give no breakout


def test_a_seed_three_intervals_old_is_dropped_as_stale_and_the_first_live_bar_still_ignored(caplog):
    live = _live_closes()

    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(_with_seed(_seed(live[0] - 3 * FIVE_MIN_NS)), prices=flat)

    assert strategy.fed == list(live[1:])
    assert ("warmup_seed_failed", "seed_stale") in _codes(caplog)
    stale = next(r for r in caplog.records if getattr(r, "fields", {}).get("reason") == "seed_stale")
    assert stale.levelname == "WARNING"


def test_a_live_bar_at_or_before_the_seed_s_last_close_is_ignored():
    live = _live_closes()
    seed = _seed(live[1])  # its last close equals the second live bar's

    _, strategy, _ = _run(_with_seed(seed), prices=flat)

    assert strategy.fed == [b.ts_ns for b in seed] + list(live[2:])


def test_without_a_seed_every_live_bar_is_fed_as_in_the_base():
    _, strategy, _ = _run(_with_seed(()), prices=flat)
    assert strategy.fed == list(_live_closes())
    assert len(strategy.fed) >= 6


def test_an_entry_signal_on_the_last_seed_candle_never_trades():
    live = _live_closes()
    # A wide channel the flat live price sits inside, and a breakout on the seed's last candle.
    first = live[0] - 6 * FIVE_MIN_NS
    seed = [BarInput(first + i * FIVE_MIN_NS, D(85000), D(86000), D(84000), D(85000), D(1)) for i in range(5)]
    seed.append(BarInput(live[0] - FIVE_MIN_NS, D(85000), D(86600), D(84000), D(86500), D(1)))
    probe = DonchianMidlineRule(entry_bars=2, stop_atr=D(1), ema_period=2)
    for bar in seed:
        probe.update(bar, D(100))
    assert probe.entry() is not None  # the seed alone ends on a long signal

    engine, strategy, rows = _run(_with_seed(seed), prices=flat)

    assert strategy.rule.ready()
    assert _decisions(rows) == []
    assert engine.cache.orders() == []


# Decision inputs (AC-12) --------------------------------------------------------------------


def test_a_decision_carries_the_rule_s_four_values_and_not_the_inner_exit_channel():
    _, _, rows = _run(lambda g, b, r: DonchianScalpR1(_config(), g, b, r))

    enter = next(d for d in _decisions(rows) if d.intent == Intent.ENTER)
    assert {"ema", "midline", "entry_high", "entry_low", "bar_close", "atr"} <= set(enter.inputs)
    assert not {"exit_high", "exit_low"} & set(enter.inputs)
    assert enter.inputs["ema"] is not None
    assert enter.inputs["midline"] is not None

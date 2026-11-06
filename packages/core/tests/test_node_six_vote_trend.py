"""covers: spec 0029 AC-2 (config), AC-3 (the BTC vote by close time, the wait, the BTC instance), AC-7 (decision
inputs), AC-8 (the coin and BTC seeds), AC-8a (the values log).

Runs ``SixVoteTrend`` in a BacktestEngine with two instruments: BTC (``PF_XBTUSD``) and a coin (``PF_ETHUSD``),
both with a trade and a quote every 10 s, on 1 minute bars (a test config lifts the 1 hour check so an hour is a
minute). A scripted rule records the BTC trend each bar was fed with; the end to end tests run the real rule
on a 300 bar seed. Needs the ``node`` extra.
"""

import asyncio
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import ETH, USD
from nautilus_trader.model.data import QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, OmsType, OrderType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Money, Price, Quantity
from test_node_bar_rule import INSTRUMENT, INSTRUMENT_ID, START, Script, _at, _ns

from trading_core.node.bar_rule_maker import MakerBarRuleConfig
from trading_core.node.six_vote_trend import SixVoteTrend, SixVoteTrendConfig
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Intent, RiskVerdict
from trading_core.recording.rows import DecisionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.six_vote import SixVoteRule

D = Decimal
MINUTE_NS = 60_000_000_000
BTC_ID = INSTRUMENT_ID
COIN_ID = InstrumentId.from_str("PF_ETHUSD.KRAKEN")
COIN = CryptoPerpetual(
    instrument_id=COIN_ID,
    raw_symbol=Symbol("PF_ETHUSD"),
    base_currency=ETH,
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


class _MinuteConfig(SixVoteTrendConfig, frozen=True, kw_only=True):
    """The node's config with the 1 hour check lifted, so tests run on 1 minute bars."""

    def __post_init__(self) -> None:
        MakerBarRuleConfig.__post_init__(self)


def _fields(**overrides):
    return {
        "env": "demo",
        "run_id": str(uuid.uuid7()),
        "instrument_id": str(COIN_ID),
        "bar_spec": "1-MINUTE-LAST",
        "atr_period": 14,
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
        "btc_instrument_id": str(BTC_ID),
    } | overrides


def _ticks(instrument_id, prices, minutes, size=lambda t: "0.0100", every_s=10):
    data, at = [], START
    for step in range(int(minutes * 60 / every_s)):
        t = step * every_s / 60
        mid, ts = prices(t), _ns(at)
        data.append(TradeTick(instrument_id, Price(mid, 0), Quantity.from_str(size(t)), AggressorSide.BUYER,
                              TradeId(f"{instrument_id.symbol}-{step}"), ts, ts))  # fmt: skip
        q = Quantity.from_str("5.0000")
        data.append(QuoteTick(instrument_id, Price(mid - 1, 0), Price(mid + 1, 0), q, q, ts, ts))
        at += timedelta(seconds=every_s)
    return data


def btc_alternating(t):
    """Above and below 80000 by turns, minute by minute, so the BTC sign flips every bar."""
    return 80100 if int(t) % 2 == 0 else 79900


SEED_VOLUME = D("0.06")


def _seed(closes, last_ns, volume=SEED_VOLUME):
    """Seed candles closing one minute apart, the last at ``last_ns``; high and low 1 either side."""
    n = len(closes)
    return tuple(
        BarInput(last_ns - (n - 1 - i) * MINUTE_NS, D(c), D(c) + 1, D(c) - 1, D(c), volume)
        for i, c in enumerate(closes)
    )


FLAT_BTC_SEED = _seed([80000] * 300, _at(1) - MINUTE_NS)  # EMA 80000, ready, last close 11:59


def _run(
    strategy_cls=SixVoteTrend,
    coin_prices=lambda t: 3000,
    btc_prices=btc_alternating,
    minutes=12,
    coin_data=None,
    seed=(),
    btc_seed=FLAT_BTC_SEED,
    **overrides,
):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(10000, USD)],
        base_currency=USD,
        trade_execution=False,
        bar_execution=False,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_instrument(COIN)
    engine.add_data(_ticks(BTC_ID, btc_prices, minutes))
    coin = coin_data if coin_data is not None else _ticks(COIN_ID, coin_prices, minutes)
    if coin:
        engine.add_data(coin)
    records: list[WriteItem] = []
    values: dict[str, Any] = _fields(**overrides)
    strategy = strategy_cls(_MinuteConfig(**values), PauseGate(), EntryBlocks(), records.append, seed, btc_seed)
    engine.add_strategy(strategy)

    async def run():
        engine.run()

    asyncio.run(run())
    return engine, strategy, [item.row for item in records]


def _decisions(rows):
    return [r for r in rows if isinstance(r, DecisionRow)]


def _logs(caplog, code):
    return [f for r in caplog.records if (f := getattr(r, "fields", {})).get("code") == code]


class BtcScript(Script):
    """A scripted rule that notes the BTC trend each bar was fed with, as ``(close ns, trend)``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.trend = 0
        self.fed = []

    def set_btc_trend(self, trend):
        self.trend = trend

    def update(self, bar, atr):
        super().update(bar, atr)
        self.fed.append((bar.ts_ns, self.trend))


class _Scripted(SixVoteTrend):
    def __init__(self, config, gate, blocks, record, seed=(), btc_seed=()):
        super().__init__(config, gate, blocks, record, seed, btc_seed)
        self.rule = self._six_rule = BtcScript()  # type: ignore[assignment]


# Config (AC-2) ------------------------------------------------------------------------------


def test_the_defaults_are_the_spec_s_settings():
    config = SixVoteTrendConfig(**{k: v for k, v in _fields().items() if k not in ("bar_spec", "atr_period")})
    assert (config.bar_spec, config.time_stop_bars, config.atr_period, config.atr_floor_mult) == (
        "1-HOUR-LAST",
        24,
        14,
        D(1),
    )
    assert (config.entry_offset_atr, config.entry_wait_bars, config.exit_wait_bars) == (D(0), 1, 1)
    assert (config.atr_min_bps, config.spread_max_atr) == (None, D("0.1"))
    assert (config.trail_atr, config.trail_min_step_atr, config.trail_min_interval_s, config.trail_max_refusals) == (
        D(3),
        D("0.1"),
        5,
        5,
    )
    assert (config.btc_instrument_id, config.btc_wait_s) == ("PF_XBTUSD.KRAKEN", 3)


def test_the_trail_switch_accepts_none():
    assert SixVoteTrendConfig(**_fields(bar_spec="1-HOUR-LAST", trail_atr=None)).trail_atr is None


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"bar_spec": "5-MINUTE-LAST"}, "bar_spec"),
        ({"trail_atr": D(0)}, "trail_atr"),
        ({"trail_atr": D(-1)}, "trail_atr"),
        ({"trail_min_step_atr": D("-0.1")}, "trail_min_step_atr"),
        ({"trail_min_interval_s": 0}, "trail_min_interval_s"),
        ({"trail_max_refusals": 0}, "trail_max_refusals"),
        ({"spread_max_atr": D(0)}, "spread_max_atr"),
        ({"btc_wait_s": -1}, "btc_wait_s"),
    ],
)
def test_a_bad_setting_is_refused(change, problem):
    with pytest.raises(ValueError, match=problem):
        SixVoteTrendConfig(**(_fields(bar_spec="1-HOUR-LAST") | change))


def test_the_node_runs_the_six_vote_rule_on_its_own_hour_bars():
    strategy = SixVoteTrend(SixVoteTrendConfig(**_fields(bar_spec="1-HOUR-LAST")), PauseGate(), EntryBlocks(), print)
    assert str(strategy.bar_type) == "PF_ETHUSD.KRAKEN-1-HOUR-LAST-INTERNAL"
    assert str(strategy._btc_bar_type) == "PF_XBTUSD.KRAKEN-1-HOUR-LAST-INTERNAL"
    assert isinstance(strategy.rule, SixVoteRule)


# The BTC vote (AC-3) ------------------------------------------------------------------------


def test_each_coin_bar_gets_the_btc_sign_of_the_same_close_time():
    _, strategy, _ = _run(_Scripted)

    fed = strategy.rule.fed[1:]  # the first live bar comes before any live BTC bar
    assert len(fed) >= 8
    for ts, trend in fed:
        assert trend == strategy._btc.sign_at(ts)
    assert {trend for _, trend in fed} == {1, -1}  # the sign flips each bar, so a bar off would show


def test_the_btc_bar_type_is_subscribed_for_a_coin():
    engine, strategy, _ = _run(_Scripted, minutes=3)
    assert strategy._btc_bar_type in engine.cache.bar_types()


class _BtcStopsAt(_Scripted):
    """BTC bars stop arriving from minute 6 on."""

    def on_bar(self, bar):
        if bar.bar_type == self._btc_bar_type and bar.ts_event >= _at(7):
            return
        super().on_bar(bar)


def test_with_no_btc_bar_within_the_wait_the_coin_bar_is_fed_with_the_last_sign(caplog):
    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(_BtcStopsAt)

    late = _logs(caplog, "btc_bar_late")
    late_ts = {f["bar_close_ns"] for f in late}
    assert {_at(7), _at(8), _at(9)} <= late_ts
    last_btc = strategy._btc.sign_at(_at(6))
    assert all(trend == last_btc for ts, trend in strategy.rule.fed if ts >= _at(7))
    # Each held bar was fed within the wait: one feed per bar close, in order.
    stamps = [ts for ts, _ in strategy.rule.fed]
    assert stamps == sorted(set(stamps))


def test_the_btc_instance_uses_its_own_bars_with_no_second_subscription():
    _, strategy, _ = _run(_Scripted, instrument_id=str(BTC_ID), coin_prices=None, coin_data=[])

    assert strategy._is_btc
    fed = strategy.rule.fed[1:]
    assert fed
    for ts, trend in fed:
        assert trend == strategy._btc.sign_at(ts)


# Seeds (AC-8) -------------------------------------------------------------------------------


def test_a_stale_btc_seed_is_dropped_so_v2_stays_zero(caplog):
    stale = _seed([80000] * 300, _at(1) - 3 * MINUTE_NS)
    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(_Scripted, btc_seed=stale)

    [dropped] = _logs(caplog, "warmup_seed_failed")
    assert (dropped["reason"], dropped["btc"]) == ("seed_stale", True)
    assert not strategy._btc.ready
    assert {trend for _, trend in strategy.rule.fed} == {0}


def test_a_fresh_btc_seed_makes_btc_ready_at_once(caplog):
    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(_Scripted)
    assert strategy._btc.ready
    assert _logs(caplog, "btc_seed_fed")


def test_a_coin_seed_three_intervals_old_is_dropped_as_stale(caplog):
    seed = _seed([3000] * 300, _at(1) - 3 * MINUTE_NS)
    with caplog.at_level(logging.INFO):
        _, strategy, _ = _run(seed=seed)

    assert [f["reason"] for f in _logs(caplog, "warmup_seed_failed")] == ["seed_stale"]
    assert not strategy.rule.ready()


# The real rule end to end (AC-7, AC-8, AC-8a) -----------------------------------------------

COIN_SEED = _seed([3000 + k for k in range(300)], _at(1) - MINUTE_NS)  # climbs to 3299 at 11:59
BTC_SEED = _seed([79700 + k for k in range(300)], _at(1) - MINUTE_NS)  # climbs, EMA200 below 80000


def coin_climb_then_jump(t):
    """Climbs 1 a minute from 3300, then jumps 20 within minute 5 and stays."""
    if t < 5:
        return int(3300 + t)
    if t < 6:
        return int(3305 + (t - 5) * 24)
    return 3325


def _jump_size(t):
    return "1.0000" if 5 <= t < 6 else "0.0100"


def test_a_seeded_rule_logs_its_values_at_every_bar_close_from_the_first_live_one(caplog):
    with caplog.at_level(logging.INFO):
        _run(seed=COIN_SEED, btc_seed=BTC_SEED, btc_prices=lambda t: 80400, minutes=5)

    logs = _logs(caplog, "six_vote_values")
    assert [f["bar_close_ns"] for f in logs] == [_at(2), _at(3), _at(4), _at(5)]
    assert {"score", "v_trend", "v_btc", "v_ema", "v_rsi", "v_breakout", "v_volume", "ema200", "rsi14"} <= set(logs[0])
    assert logs[-1]["v_btc"] == 1
    assert logs[-1]["instrument"] == str(COIN_ID)


def test_all_six_votes_on_a_live_jump_bar_enter_and_the_decision_carries_them():
    data = _ticks(COIN_ID, coin_climb_then_jump, 9, size=_jump_size)
    engine, _, rows = _run(
        seed=COIN_SEED, btc_seed=BTC_SEED, btc_prices=lambda t: 80400, coin_data=data, minutes=9, spread_max_atr=None
    )

    [enter] = _decisions(rows)
    inputs = enter.inputs
    assert (enter.intent, enter.reason, enter.risk_verdict) == (Intent.ENTER, "six_vote", RiskVerdict.APPROVED)
    assert inputs["score"] == 6
    assert [inputs[k] for k in ("v_trend", "v_btc", "v_ema", "v_rsi", "v_breakout", "v_volume")] == [1] * 6
    assert {"bar_close", "atr", "ema9", "ema21", "ema200", "rsi14"} <= set(inputs)
    close, atr = D(str(inputs["bar_close"])), D(str(inputs["atr"]))
    assert D(str(inputs["structural_stop"])) == close - 3 * atr
    assert D(str(inputs["limit_px"])) == close  # offset 0: the limit rests at the signal close
    [entry] = [o for o in engine.cache.orders() if o.order_type == OrderType.LIMIT]
    assert entry.is_post_only


def test_the_spread_check_records_spread_wide():
    data = _ticks(COIN_ID, coin_climb_then_jump, 9, size=_jump_size)
    engine, _, rows = _run(seed=COIN_SEED, btc_seed=BTC_SEED, btc_prices=lambda t: 80400, coin_data=data, minutes=9,
                           spread_max_atr=D("0.1"))  # fmt: skip

    [refused] = _decisions(rows)
    assert (refused.risk_verdict, refused.risk_reason) == (RiskVerdict.REJECTED, "spread_wide")
    assert refused.inputs["score"] == 6
    assert D(str(refused.inputs["spread"])) > D(str(refused.inputs["spread_limit"]))
    assert engine.cache.orders() == []

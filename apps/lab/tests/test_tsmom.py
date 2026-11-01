"""covers: spec 0027.1 AC-10 (the lab admits kind ``trend``, ``lab screen`` refuses it) and AC-10a (the warmup pre
roll: bars and indicators only, nothing traded or recorded before the segment start); spec 0027.2 AC-14 (the
registry entry), AC-15 (the SOL and XRP pins), and AC-17 (four coins in one account on daily bars).

The four coin fixture is hand built: every minute of a day trades at that day's price (high and low 1 away),
so each daily bar's close is known. The segment starts on day 5 and ``tsmom`` (lookback 2, ATR 2) needs 5
bars, so the whole pre roll is days 0 to 4:

- ETH: flat, then up on day 4: a long at the segment's first close (only a pre roll makes that possible),
  then ``momentum_exit`` on day 8's bar after day 7 falls back.
- SOL: the same entry, a crash on day 5 fills the stop, and the next close (day 6's bar) enters again.
- XRP: rising through the pre roll (signals there are dropped), a long at the segment start, then a
  ``momentum_exit`` and a falling market with no short ever.
- BTC: flat all along, so never an entry (a close equal to the lagged close is not above it).
"""

import tomllib
from dataclasses import replace
from decimal import Decimal

import pytest

from lab.candle_fetch import candle_tree
from lab.candle_units import CandleUnitPlan, run_candle_unit
from lab.instruments import is_pinned, pinned_instrument
from lab.runfile import Overrides, RunFileError, candle_run_from_raw
from lab.strategies import REGISTRY, MarketContextValues, registry_entry
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month, MonthManifest
from trading_core.lab.pre_roll import pre_roll_start
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import Intent, OrderSide, OrderType, PositionSide, StrategyKind
from trading_core.recording.rows import ns_to_utc
from trading_core.risk.check import RiskLimits

from .candle_fixtures import FEB, synthetic_month
from .test_lookahead import _prefix_mismatches

D = Decimal
BTC, ETH, SOL, XRP = "PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN", "PF_XRPUSD.KRAKEN"
SEP = Month(2026, 9)
T0 = SEP.start_ns
DAY_NS = 24 * HOUR_NS
DAYS = 12
START = T0 + 5 * DAY_NS
RISK = RiskLimits(
    max_order_notional=D(20000),
    max_leverage=D(2),
    max_total_exposure=D(20000),
    risk_per_trade=D("0.005"),
    maintenance_margin_rate=D("0.01"),
    min_liq_distance_pct=D(10),
    liq_stop_multiple=D(3),
    stop_slippage_bps=D(5),
)
CONFIG: dict[str, int | str | bool] = {
    "bar_spec": "1-DAY-LAST",
    "atr_period": 2,
    "atr_floor_mult": "1",
    "time_stop_bars": 30,
    "stop_atr": "2",
    "lookback_days": 2,
    "long_only": True,
}
CLOSES = {
    ETH: [100, 100, 100, 100, 110, 111, 112, 105, 105, 105, 105, 105],
    SOL: [100, 100, 100, 100, 110, 112, 113, 114, 115, 116, 117, 118],
    XRP: [100, 101, 102, 103, 104, 103, 101, 95, 90, 85, 80, 75],
    BTC: [100] * DAYS,
}
# SOL's day 5 crash: one minute at 10:00 trades down to 90, through the stop.
CRASH = {SOL: (5, 600, D(90))}
RUN = """
instrument_ids = ["PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN", "PF_XBTUSD.KRAKEN", "PF_XRPUSD.KRAKEN"]
from = 2026-09-06
to = 2026-09-12
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"
max_total_exposure = "20000"

[strategy]
name = "tsmom"
[strategy.config]
bar_spec = "1-DAY-LAST"
atr_period = 20
time_stop_bars = 30
stop_atr = "3"
long_only = true
"""


def _rows(coin):
    rows = []
    crash = CRASH.get(coin)
    for day, close in enumerate(CLOSES[coin]):
        p = D(close)
        for minute in range(24 * 60):
            low = crash[2] if crash is not None and (day, minute) == crash[:2] else p - 1
            rows.append(CandleRow(T0 + day * DAY_NS + (minute + 1) * MINUTE_NS, p, p + 1, low, p, D(1), False))
    return rows


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """Twelve hand built days of the four coins, with hourly funding at 3 bps a day."""
    root = tmp_path_factory.mktemp("catalog")
    for coin in CLOSES:
        rows = _rows(coin)
        tree = candle_tree(root, coin)
        tree.write_bars(SEP, rows)
        rate = D("0.0000125")
        tree.write_funding(SEP, [FundingRow(T0 + h * HOUR_NS, rate, HOUR_NS, "api") for h in range(DAYS * 24 + 1)])
        tree.write_gaps(SEP, [])
        manifest = MonthManifest(
            "2026-09",
            len(rows),
            0,
            0,
            0,
            0,
            0,
            [],
            rows[0].ts_ns,
            rows[-1].ts_ns,
            0,
            0,
            0,
            False,
            "2026-10-01T00:00:00Z",
        )
        tree.write_manifest(manifest.with_funding(tree.read_funding(SEP)))
    return root


def _plan(catalog, **overrides):
    plan = CandleUnitPlan(
        variant="base",
        candles_root=catalog / "candles",
        instrument_ids=(ETH, SOL, BTC, XRP),
        start_ns=START,
        end_ns=T0 + DAYS * DAY_NS,
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        slippage_bps=D(2),
        starting_balance_usd=D(10000),
        leverage=D(2),
        risk=RISK,
        seed=7,
        strategy_name="tsmom",
        strategy_config=CONFIG,
        stale_quote_s=D(300),
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )
    return replace(plan, **overrides)


@pytest.fixture(scope="module")
def unit(catalog):
    return run_candle_unit(_plan(catalog))


def _decisions(unit, coin, intent=None):
    return [r for _, r in unit.decisions if r.instrument_id == coin and (intent is None or r.intent == intent)]


def _day(n):
    return T0 + n * DAY_NS


# AC-17: the daily bars, the rule's trades, and one account -------------------------------------


def test_each_daily_bar_closes_on_the_day_before_and_the_first_entry_is_the_segments_first_close(unit):
    for coin in (ETH, SOL, XRP):
        first = _decisions(unit, coin, Intent.ENTER)[0]
        # The bar stamped 00:00 on day 5 holds day 4's minutes only: its close is day 4's price, not day 5's.
        assert (first.decided_ns // DAY_NS * DAY_NS, first.inputs["bar_close"]) == (START, str(CLOSES[coin][4]))
        assert first.decided_ns >= START
        assert first.reason == "tsmom_long"
        assert first.inputs["close_lag"] == str(CLOSES[coin][2])


def test_eth_enters_long_and_exits_on_momentum_at_the_first_close_at_or_below_the_lag(unit):
    exits = _decisions(unit, ETH, Intent.EXIT)
    assert [e.reason for e in exits] == ["momentum_exit"]
    # Day 7's close (105) is under day 5's (111): the exit is on the bar stamped day 8.
    assert exits[0].decided_ns // DAY_NS * DAY_NS == _day(8)
    assert len(_decisions(unit, ETH, Intent.ENTER)) == 1  # 105 never rises above its lag again


def test_sol_is_stopped_out_and_enters_again_on_the_next_qualifying_close(unit):
    entries = _decisions(unit, SOL, Intent.ENTER)
    assert [e.decided_ns // DAY_NS * DAY_NS for e in entries[:2]] == [_day(5), _day(6)]
    sol_positions = [r for _, r in unit.positions if r.instrument_id == SOL]
    assert len({(p.opened_at) for p in sol_positions}) >= 2


def test_xrp_never_shorts_in_a_falling_market_and_btc_never_enters_on_an_equal_close(unit):
    entry_orders = [r for _, r in unit.orders if r.decision_id in _entry_ids(unit) and r.order_type == OrderType.MARKET]
    assert entry_orders
    assert {r.side for r in entry_orders} == {OrderSide.BUY}  # the stops are the entries' only sells
    assert [e.reason for e in _decisions(unit, XRP, Intent.EXIT)] == ["momentum_exit"]
    assert len(_decisions(unit, XRP, Intent.ENTER)) == 1
    assert _decisions(unit, BTC) == []
    assert all(d.side in (None, OrderSide.BUY) for _, d in unit.decisions if d.intent == Intent.ENTER)
    assert all(p.side != PositionSide.SHORT for _, p in unit.positions)


def _entry_ids(unit):
    return {d.id for _, d in unit.decisions if d.intent == Intent.ENTER}


def test_the_coins_trade_in_one_account(unit):
    coins = {r.instrument_id for _, r in unit.positions}
    assert coins == {ETH, SOL, XRP}
    assert set(unit.per_instrument) == {BTC, ETH, SOL, XRP}


# AC-10a: the pre roll ----------------------------------------------------------------------------


def test_nothing_is_decided_ordered_filled_or_held_before_the_segment_start(unit):
    start = ns_to_utc(START)
    assert all(r.decided_at >= start for _, r in unit.decisions)
    assert all(r.occurred_at >= start for _, r in unit.order_events)
    assert all(r.filled_at >= start for _, r in unit.fills)
    assert all(r.opened_at >= start for _, r in unit.positions)
    assert all(r.occurred_at >= start for _, r in unit.risk_events)
    assert all(s.boundary_ns >= START for samples in unit.funding_samples.values() for s in samples)
    assert all(f.first_trade_ns is not None and f.first_trade_ns >= START for f in unit.per_instrument.values())


def test_the_pre_roll_starts_warmup_bars_before_and_never_before_the_first_stored_month():
    stored = {Month(2026, 9), Month(2026, 10)}.__contains__
    oct1 = Month(2026, 10).start_ns
    assert pre_roll_start(oct1 + 10 * DAY_NS, 5, DAY_NS, stored) == oct1 + 5 * DAY_NS
    assert pre_roll_start(oct1 + 10 * DAY_NS, 81, DAY_NS, stored) == Month(2026, 9).start_ns
    assert pre_roll_start(oct1, 0, DAY_NS, stored) == oct1
    with pytest.raises(ValueError, match="warmup_bars"):
        pre_roll_start(oct1, -1, DAY_NS, stored)


def test_an_hour_level_unit_keeps_its_cold_start(catalog):
    """buy_and_hold (kind hour_level) gets no pre roll: its first fill is at the segment start, as before."""
    unit = run_candle_unit(
        _plan(
            catalog,
            instrument_ids=(ETH,),
            end_ns=_day(7),
            strategy_name="buy_and_hold",
            strategy_config={"notional_usd": "5000"},
        )
    )
    assert min(r.filled_at for _, r in unit.fills) >= ns_to_utc(START)
    assert unit.per_instrument[ETH].first_trade_ns == START


# AC-10, AC-14, AC-15: the run file, the registry, and the pins ------------------------------------


def test_the_registry_entry_is_kind_trend_from_its_own_module():
    entry = registry_entry("tsmom")
    assert entry.kind == StrategyKind.TREND
    assert entry.module == "trading_core.node.tsmom"
    assert (entry.candle_screen, entry.candle_gate) == (False, False)
    assert [n for n, e in REGISTRY.items() if e.kind == StrategyKind.TREND] == ["tsmom"]


def test_a_trend_run_file_loads_with_its_four_coins_and_its_config_alone_passes_the_check():
    loaded = candle_run_from_raw("run.toml", b"", tomllib.loads(RUN), Overrides(), allow_multi=True)
    assert loaded.run.instruments == (ETH, SOL, BTC, XRP)
    registry_entry("tsmom").check_strategy(dict(loaded.run.strategy.config))  # lookback_days from the default


def test_a_trend_run_file_with_a_short_side_is_refused():
    raw = tomllib.loads(RUN)
    raw["strategy"]["config"]["long_only"] = False
    with pytest.raises(RunFileError, match="long_only"):
        candle_run_from_raw("run.toml", b"", raw, Overrides(), allow_multi=True)


@pytest.mark.parametrize(
    ("coin", "tick", "step", "margins"),
    [(SOL, "0.01", "0.01", ("0.01", "0.005")), (XRP, "0.00001", "1", ("0.02", "0.01"))],
)
def test_the_sol_and_xrp_pins_load(coin, tick, step, margins):
    instrument = pinned_instrument(coin)
    assert (str(instrument.price_increment), str(instrument.size_increment)) == (tick, step)
    assert (str(instrument.margin_init), str(instrument.margin_maint)) == margins
    assert (str(instrument.maker_fee), str(instrument.taker_fee)) == ("0.0002", "0.0005")
    assert not is_pinned("PF_DOGEUSD.KRAKEN")


def test_the_xrp_pin_holds_the_historical_tick_and_todays_prices_sit_on_it():
    """AC-15 amendment: tick 0.00001, precision 5; an old tick price and a 0.0001 price are both on tick."""
    instrument = pinned_instrument(XRP)
    tick = instrument.price_increment.as_decimal()
    assert (tick, instrument.price_precision) == (Decimal("0.00001"), 5)
    for price in (Decimal("0.53217"), Decimal("2.4581")):
        assert price % tick == 0
        assert instrument.make_price(price).as_decimal() == price


# AC-17: no daily bar holds a minute after its timestamp (the --lookahead-check method) -------------


def test_tsmom_decides_the_same_before_each_of_three_cuts(tmp_path):
    """The look ahead prefix check (spec 0014 AC-19) on the seeded synthetic month, with a 5 day pre roll."""
    synthetic_month(tmp_path, seed=7, days=10)  # its daily closes rise on 4 of the 5 segment days
    plan = _plan(
        tmp_path,
        instrument_ids=(BTC,),
        start_ns=FEB.start_ns + 5 * DAY_NS,
        end_ns=FEB.start_ns + 10 * DAY_NS,
        strategy_config={**CONFIG, "lookback_days": 1, "stop_atr": "1"},
    )
    full, mismatches = _prefix_mismatches(plan, 3)
    assert full, "tsmom made no decision over the synthetic days"
    assert mismatches == []

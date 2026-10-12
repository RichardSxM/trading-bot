"""covers: spec 0009 AC-11 (KfsStrategy reads the market context through the same handlers live and in
a replay; PlaceholderTimed logs it once per bar).

Runs ``PlaceholderTimed`` in a BacktestEngine (see ``test_node_placeholder``), plus a ``RandomTaker``
registered with a live clock for the live look ahead rule. Needs the ``node`` extra.
"""

import logging
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.common.component import LiveClock
from nautilus_trader.model.data import FundingRateUpdate
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.test_kit.stubs.component import TestComponentStubs
from test_node_placeholder import INSTRUMENT_ID, START, Prices, _ns, _run

from trading_core.marketdata.context import ContextLookAheadError
from trading_core.node.random_taker import RandomTaker, RandomTakerConfig
from trading_core.node.strategy import PauseGate
from trading_core.strategy.blocks import EntryBlocks

SECOND_NS = 1_000_000_000
FLAT = Prices(10, lambda t: 80_000)


def _funding(every_s=30, minutes=10):
    rows = []
    for i in range(minutes * 60 // every_s):
        ts = _ns(START) + i * every_s * SECOND_NS + 5 * SECOND_NS
        rows.append(
            FundingRateUpdate(
                INSTRUMENT_ID, Decimal(i) / Decimal(10**8), ts, ts, next_funding_ns=_ns(START) + 3_600 * SECOND_NS
            )
        )
    return rows


def _context_logs(caplog):
    return [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == "market_context"]


def test_a_replayed_strategy_reads_funding_known_at_each_bar(caplog):
    caplog.set_level(logging.INFO)
    funding = _funding()

    _, strategy, _ = _run(FLAT, extra_data=funding, use_market_context=True)

    logs = _context_logs(caplog)
    assert len(logs) >= 8  # one per 1 minute bar
    # The first bar closes at 12:00:00, before the first rate (12:00:05): nothing is known yet.
    assert logs[0]["funding_rate"] is None
    for minute, fields in enumerate(logs[1:], start=1):
        # The newest rate at or before the bar: updates come every 30 s from 12:00:05, so the
        # bar at minute m sees update 2m - 1, 25 s old. Never one from after the bar.
        assert Decimal(fields["funding_rate"]) == Decimal(2 * minute - 1) / Decimal(10**8)
        assert fields["funding_rate_age_s"] == "25.000"
        assert fields["funding_rate_stale"] is False
        assert fields["mark"] is None
    assert strategy.market_context().funding_rate.value == Decimal(len(funding) - 1) / Decimal(10**8)


def test_without_the_option_nothing_is_subscribed_or_logged(caplog):
    caplog.set_level(logging.INFO)

    _, strategy, _ = _run(FLAT, extra_data=_funding())

    assert _context_logs(caplog) == []
    with pytest.raises(RuntimeError, match="use_market_context"):
        strategy.market_context()


def test_a_look_ahead_in_a_replay_raises():
    _, strategy, _ = _run(FLAT, extra_data=_funding(), use_market_context=True)
    future = strategy.clock.timestamp_ns() + SECOND_NS
    strategy._context.apply_funding(Decimal(1), None, future)

    with pytest.raises(ContextLookAheadError):
        strategy.market_context()


def _live_strategy():
    config = RandomTakerConfig(
        env="demo",
        run_id="00000000-0000-0000-0000-000000000000",
        instrument_id=str(INSTRUMENT_ID),
        seed=1,
        max_order_notional_usd=Decimal(1),
        max_leverage=Decimal(1),
        use_market_context=True,
    )
    strategy = RandomTaker(config, PauseGate(), EntryBlocks(), lambda item: None)
    clock = LiveClock()
    msgbus = TestComponentStubs.msgbus()
    cache = TestComponentStubs.cache()
    portfolio = TestComponentStubs.portfolio()
    strategy.register(TraderId("TESTER-001"), portfolio, msgbus, cache, clock)
    return strategy


def test_live_a_clock_behind_the_latest_update_snapshots_at_that_update_and_logs_once(caplog):
    caplog.set_level(logging.WARNING)
    strategy = _live_strategy()
    ahead = strategy.clock.timestamp_ns() + 3_600 * SECOND_NS
    strategy._context.apply_funding(Decimal("0.0001"), None, ahead)

    first = strategy.market_context()
    strategy.market_context()

    assert first.funding_rate.value == Decimal("0.0001")
    assert first.funding_rate.is_stale is False
    behind = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "context_clock_behind"]
    assert len(behind) == 1


def test_mark_and_index_give_the_basis_at_each_bar(caplog):
    from nautilus_trader.model.data import IndexPriceUpdate, MarkPriceUpdate
    from nautilus_trader.model.objects import Price

    caplog.set_level(logging.INFO)
    rows = []
    for i in range(60):
        ts = _ns(START) + i * 10 * SECOND_NS + SECOND_NS
        rows.append(MarkPriceUpdate(INSTRUMENT_ID, Price(83_597, 0), ts, ts))
        rows.append(IndexPriceUpdate(INSTRUMENT_ID, Price(83_587, 0), ts, ts))

    _run(FLAT, extra_data=rows, use_market_context=True)

    logs = [f for f in _context_logs(caplog) if f["mark"] is not None]
    assert len(logs) >= 8
    for fields in logs:
        assert (fields["mark"], fields["index"]) == ("83597", "83587")
        assert Decimal(fields["basis"]) == Decimal(10) / Decimal(83_587)
        assert fields["basis_stale"] is False
        assert fields["mark_age_s"] == "9.000"  # the last row at :51, the bar at :00


def test_open_interest_and_liquidations_arrive_as_custom_data(caplog):
    from nautilus_trader.model.data import CustomData
    from nautilus_trader.model.objects import Price, Quantity

    from trading_core.marketdata.context import AggressorSide, LiquidationKind
    from trading_core.node.context_types import LiquidationTick, OpenInterestUpdate, context_data_type

    caplog.set_level(logging.INFO)
    rows = []
    for i in range(60):
        ts = _ns(START) + i * 10 * SECOND_NS + SECOND_NS
        oi = OpenInterestUpdate(INSTRUMENT_ID, Quantity.from_str("2423.1378"), ts, ts)
        rows.append(CustomData(context_data_type(OpenInterestUpdate, INSTRUMENT_ID), oi))
        if i % 6 == 0:
            tick = LiquidationTick(
                INSTRUMENT_ID,
                f"u{i}",
                LiquidationKind.LIQUIDATION,
                AggressorSide.SELL,
                Price(80_000, 0),
                Quantity.from_str("0.5000"),
                ts,
                ts,
            )
            rows.append(CustomData(context_data_type(LiquidationTick, INSTRUMENT_ID), tick))

    _run(FLAT, extra_data=(), use_market_context=True, custom_data=rows)

    logs = [f for f in _context_logs(caplog) if f["open_interest"] is not None]
    assert len(logs) >= 8
    for fields in logs:
        assert fields["open_interest"] == "2423.1378"
        assert fields["liquidations_known"] is True
        # One liquidation each minute, at :01; at the bar (:00) the window (59 s] holds none or one.
        assert Decimal(fields["liquidations_sold_size"]) in (0, Decimal("0.5"))
    assert any(Decimal(f["liquidations_sold_notional"]) == 40_000 for f in logs)

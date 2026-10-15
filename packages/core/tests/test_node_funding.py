"""covers: spec 0011 AC-13 (demo funding booked at the hourly boundary), AC-16 (the booked message), AC-17 (the ledger).

Runs ``RandomTaker`` with the real ``RecordingActor`` in a BacktestEngine (TestClock, simulated
venue) on the synthetic quotes of ``test_node_placeholder``, from 12:00 UTC. Needs the ``node`` extra.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import FundingRateUpdate
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import Venue
from nautilus_trader.model.objects import Money
from test_node_placeholder import INSTRUMENT, INSTRUMENT_ID, START, Prices, _ns, _quotes, _risk_events

from trading_core.node.funding import FUNDING_BOOKED_TOPIC, FundingBooked, FundingLedger
from trading_core.node.random_taker import RandomTaker, RandomTakerConfig
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env, FundingSource, RiskEventKind, Severity
from trading_core.recording.rows import FundingRow, PositionRow
from trading_core.recording.spool import WriteItem
from trading_core.strategy.blocks import EntryBlocks

SECOND_NS = 1_000_000_000
BOUNDARY_NS = _ns(START) + 3_600 * SECOND_NS  # 13:00 UTC
RATE = Decimal("0.0001")
# 80 minutes of quotes, so 13:00 falls inside the run.
STEADY = Prices(80, lambda t: 85000)


def _rates(rate=RATE, every_s=300, minutes=80):
    rows = []
    for i in range(minutes * 60 // every_s):
        ts = _ns(START) + i * every_s * SECOND_NS + 7 * SECOND_NS
        rows.append(FundingRateUpdate(INSTRUMENT_ID, rate, ts, ts, next_funding_ns=BOUNDARY_NS))
    return rows


class _Listener(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Collects what the recording actor publishes on ``FUNDING_BOOKED_TOPIC``."""

    def __init__(self):
        super().__init__(ActorConfig(component_id="FundingListener"))
        self.booked = []

    def on_start(self):
        self.msgbus.subscribe(topic=FUNDING_BOOKED_TOPIC, handler=self.booked.append)


def _run(prices=STEADY, *, interval_s=1_800, hold_s=3_600, rates=None):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"),
        OmsType.NETTING,
        AccountType.MARGIN,
        starting_balances=[Money(1000, USD)],
        base_currency=USD,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(_quotes(prices))
    if rates:
        engine.add_data(rates)
    run_id = uuid.uuid7()
    records: list[WriteItem] = []
    blocks = EntryBlocks()
    ledger = FundingLedger()
    listener = _Listener()
    engine.add_actor(listener)
    engine.add_actor(
        RecordingActor(
            env=Env.DEMO,
            run_id=run_id,
            instrument_id=INSTRUMENT_ID,
            record=records.append,
            blocks=blocks,
            maintenance_margin_rate=Decimal("0.01"),
            min_liq_distance_pct=Decimal(10),
            stale_quote_s=Decimal(10),
            ledger=ledger,
        )
    )
    config = RandomTakerConfig(
        env="demo",
        run_id=str(run_id),
        instrument_id=str(INSTRUMENT_ID),
        seed=1,
        max_order_notional_usd=Decimal(250),
        max_leverage=Decimal(2),
        max_total_exposure_usd=Decimal(500),
        risk_per_trade=Decimal("0.005"),
        maintenance_margin_rate=Decimal("0.01"),
        min_liq_distance_pct=Decimal(10),
        liq_stop_multiple=Decimal(3),
        stop_slippage_bps=Decimal(5),
        taker_fee=Decimal("0.0005"),
        notional_usd=Decimal(100),
        interval_s=interval_s,
        hold_s=hold_s,
    )
    engine.add_strategy(RandomTaker(config, PauseGate(), blocks, records.append))

    async def run():
        engine.run()

    asyncio.run(run())
    return [item.row for item in records], ledger, listener.booked


def _funding(rows):
    return [r for r in rows if isinstance(r, FundingRow)]


def test_a_position_open_across_the_hour_books_one_payment_with_the_right_sign():
    # Entered 12:30, held an hour: open across 13:00.
    rows, ledger, booked = _run(rates=_rates())

    (payment,) = _funding(rows)
    position = next(r for r in rows if isinstance(r, PositionRow) and r.id == payment.position_id)
    signed_qty = position.quantity  # signed
    assert payment.paid_ns == BOUNDARY_NS
    assert (payment.source, payment.rate, payment.currency) == (FundingSource.COMPUTED, RATE, "USD")
    # A long pays a positive rate, a short earns it: amount = -signed qty * mid * rate.
    assert payment.amount == -signed_qty * Decimal(85000) * RATE
    assert (payment.amount < 0) == (signed_qty > 0)
    assert ledger.run_total == payment.amount
    assert booked == [FundingBooked(INSTRUMENT_ID.value, BOUNDARY_NS, payment.position_id, payment.amount)]


def test_a_position_that_opens_exactly_at_the_boundary_is_not_charged():
    # One entry an hour, at 13:00:00 exactly.
    rows, ledger, booked = _run(interval_s=3_600, hold_s=600, rates=_rates())

    assert any(isinstance(r, PositionRow) for r in rows)
    assert _funding(rows) == []
    assert (ledger.run_total, booked) == (Decimal(0), [])


def test_a_position_that_closes_before_the_boundary_is_not_charged():
    # Entered 12:30, closed 10 s before 13:00; the 13:00 entry opens on the boundary.
    # (Closing exactly on it is covered by ``test_risk_funding.test_who_is_charged``.)
    rows, ledger, _ = _run(hold_s=1_790, rates=_rates())

    assert _funding(rows) == []
    assert ledger.run_total == Decimal(0)


def test_no_rate_books_nothing_and_says_so_once():
    rows, ledger, booked = _run()

    assert (_funding(rows), ledger.run_total, booked) == ([], Decimal(0), [])
    (event,) = [e for e in _risk_events(rows) if e.event_key.startswith("funding_unbooked:")]
    assert event.event_key == f"funding_unbooked:{INSTRUMENT_ID.value}:{BOUNDARY_NS}"
    assert (event.kind, event.severity, event.details["rate_missing"]) == (
        RiskEventKind.LIMIT_HIT,
        Severity.WARNING,
        True,
    )


def test_a_rate_older_than_one_period_is_not_used():
    # A single rate at 11:59:00, more than an hour before 13:00.
    old = _ns(START) - 60 * SECOND_NS
    rows, _, _ = _run(rates=[FundingRateUpdate(INSTRUMENT_ID, RATE, old, old, next_funding_ns=BOUNDARY_NS)])

    assert _funding(rows) == []
    assert [e for e in _risk_events(rows) if e.event_key.startswith("funding_unbooked:")]

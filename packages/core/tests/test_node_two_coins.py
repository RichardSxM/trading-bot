"""covers: spec 0025 AC-18 (one recording actor for every coin), AC-19 (risk across coins, PEPE sizing), AC-16
(a halt flattens a PEPE position).

Runs the real ``RecordingActor`` with one strategy per coin in a BacktestEngine (TestClock, simulated venue)
on synthetic trades and quotes for a PF_XBTUSD shaped instrument (tick 1) and a PF_PEPEUSD shaped one (tick
1e-10, step 1000), from 12:00 UTC. The funding tests use ``RandomTaker``; the risk tests use ``DonchianScalpR1``
with a scripted rule. Needs the ``node`` extra.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig, LoggingConfig
from nautilus_trader.model.currencies import BTC, USD
from nautilus_trader.model.data import FundingRateUpdate, QuoteTick, TradeTick
from nautilus_trader.model.enums import AccountType, AggressorSide, CurrencyType, OmsType
from nautilus_trader.model.identifiers import InstrumentId, PositionId, Symbol, TradeId, Venue
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.model.objects import Currency, Money, Price, Quantity

from trading_core.node import recording
from trading_core.node.donchian_scalp_r1 import DonchianScalpR1, DonchianScalpR1Config
from trading_core.node.funding import FundingLedger
from trading_core.node.random_taker import RandomTaker, RandomTakerConfig
from trading_core.node.recording import RecordingActor
from trading_core.node.strategy import PauseGate
from trading_core.recording.enums import Env, Intent, RiskVerdict
from trading_core.recording.rows import (
    DecisionRow,
    FundingRow,
    InstrumentRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    RiskEventRow,
)
from trading_core.recording.spool import WriteItem
from trading_core.strategy.bar_rule import EnterSignal, Side
from trading_core.strategy.blocks import BlockReason, EntryBlocks

D = Decimal
SECOND_NS = 1_000_000_000
START = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
BTC_ID = InstrumentId.from_str("PF_XBTUSD.KRAKEN")
PEPE_ID = InstrumentId.from_str("PF_PEPEUSD.KRAKEN")
PEPE_COIN = Currency("PEPE", 8, 0, "Pepe", CurrencyType.CRYPTO)
TICK = D("1E-10")
PEPE_MID = D("0.0000123456")
BTC_MID = 85000


def _ns(at):
    return int(at.timestamp() * 1_000_000_000)


BOUNDARY_NS = _ns(START) + 3_600 * SECOND_NS  # 13:00 UTC


def _perpetual(instrument_id, base, price_precision, size_precision, tick, step):
    return CryptoPerpetual(
        instrument_id=instrument_id,
        raw_symbol=Symbol(instrument_id.symbol.value),
        base_currency=base,
        quote_currency=USD,
        settlement_currency=USD,
        is_inverse=False,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(tick),
        size_increment=Quantity.from_str(step),
        margin_init=D("0.02"),
        margin_maint=D("0.01"),
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        ts_event=0,
        ts_init=0,
    )


BTC_PERP = _perpetual(BTC_ID, BTC, 0, 4, "1", "0.0001")
PEPE_PERP = _perpetual(PEPE_ID, PEPE_COIN, 10, 0, "0.0000000001", "1000")


def _data(minutes, every_s=10, pepe=True):
    """A trade (swinging three ticks either side of the mid, so the ATR is above zero) and a quote
    (one tick either side of the mid) every ``every_s`` seconds, for each coin."""
    data, at = [], START
    for step in range(int(minutes * 60 / every_s)):
        ts = _ns(at)
        coins = [(BTC_ID, D(BTC_MID), D(1), 0, "0.0100")]
        if pepe:
            coins.append((PEPE_ID, PEPE_MID, TICK, 10, "1000"))
        for instrument_id, mid, tick, precision, size in coins:
            data.append(
                TradeTick(
                    instrument_id,
                    Price(mid + (step % 3 - 1) * 3 * tick, precision),
                    Quantity.from_str(size),
                    AggressorSide.BUYER,
                    TradeId(f"{instrument_id.symbol}-{step}"),
                    ts,
                    ts,
                )
            )
            data.append(
                QuoteTick(
                    instrument_id=instrument_id,
                    bid_price=Price(mid - tick, precision),
                    ask_price=Price(mid + tick, precision),
                    bid_size=Quantity.from_str("1000000000" if instrument_id == PEPE_ID else "5.0000"),
                    ask_size=Quantity.from_str("1000000000" if instrument_id == PEPE_ID else "5.0000"),
                    ts_event=ts,
                    ts_init=ts,
                )
            )
        at += timedelta(seconds=every_s)
    return data


def _rates(instrument_id, rate, minutes=80, every_s=300):
    rows = []
    for i in range(minutes * 60 // every_s):
        ts = _ns(START) + i * every_s * SECOND_NS + 7 * SECOND_NS
        rows.append(FundingRateUpdate(instrument_id, rate, ts, ts, next_funding_ns=BOUNDARY_NS))
    return rows


def _engine(minutes, balance=10000, pepe=True):
    engine = BacktestEngine(BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    engine.add_venue(
        Venue("KRAKEN"), OmsType.NETTING, AccountType.MARGIN, starting_balances=[Money(balance, USD)], base_currency=USD
    )
    engine.add_instrument(BTC_PERP)
    engine.add_instrument(PEPE_PERP)
    engine.add_data(_data(minutes, pepe=pepe))
    return engine


def _recording(run_id, records, blocks, coins=(BTC_ID, PEPE_ID)):
    return RecordingActor(
        env=Env.DEMO,
        run_id=run_id,
        instrument_ids=list(coins),
        record=records.append,
        blocks=blocks,
        maintenance_margin_rate=D("0.01"),
        min_liq_distance_pct=D(10),
        stale_quote_s=D(10),
        ledger=FundingLedger(),
    )


def _risk_fields(**overrides):
    return {
        "max_order_notional_usd": D(1000),
        "max_leverage": D(5),
        "max_total_exposure_usd": D(50000),
        "risk_per_trade": D("0.005"),
        "maintenance_margin_rate": D("0.01"),
        "min_liq_distance_pct": D(10),
        "liq_stop_multiple": D(3),
        "stop_slippage_bps": D(5),
        "taker_fee": D("0.0005"),
    } | overrides


def _run(engine):
    async def run():
        engine.run()

    asyncio.run(run())


def _rows(records, kind):
    return [item.row for item in records if isinstance(item.row, kind)]


# AC-18: recording -----------------------------------------------------------------------------


def _taker(run_id, instrument_id, blocks, records, hold_s=3_600):
    config = RandomTakerConfig(
        order_id_tag=instrument_id.symbol.value.removeprefix("PF_"),
        env="demo",
        run_id=str(run_id),
        instrument_id=str(instrument_id),
        seed=1,
        notional_usd=D(100),
        interval_s=1_800,
        hold_s=hold_s,
        **_risk_fields(),
    )
    return RandomTaker(config, PauseGate(), blocks, records.append)


def _funding_run(rates):
    engine = _engine(80)
    for rows in rates:
        engine.add_data(rows)
    records: list[WriteItem] = []
    run_id, blocks = uuid.uuid7(), EntryBlocks()
    engine.add_actor(_recording(run_id, records, blocks))
    for instrument_id in (BTC_ID, PEPE_ID):
        engine.add_strategy(_taker(run_id, instrument_id, blocks, records))
    _run(engine)
    return records, blocks


def test_every_coin_gets_an_instrument_row_before_any_order_and_each_event_is_recorded_once():
    records, blocks = _funding_run([])

    kinds = [type(item.row) for item in records]
    first_order = kinds.index(OrderRow)
    assert {r.instrument_id for r in _rows(records[:first_order], InstrumentRow)} == {BTC_ID.value, PEPE_ID.value}
    events = [r.event_id for r in _rows(records, OrderEventRow)]
    assert len(events) == len(set(events))
    assert {r.instrument_id for r in _rows(records, PositionRow)} == {BTC_ID.value, PEPE_ID.value}
    assert blocks.active() == ()


def test_one_boundary_books_each_coin_with_its_own_rate():
    records, _ = _funding_run([_rates(BTC_ID, D("0.0001")), _rates(PEPE_ID, D("0.0003"))])

    payments = {r.instrument_id: r for r in _rows(records, FundingRow)}
    assert set(payments) == {BTC_ID.value, PEPE_ID.value}
    assert (payments[BTC_ID.value].rate, payments[PEPE_ID.value].rate) == (D("0.0001"), D("0.0003"))
    assert {p.paid_ns for p in payments.values()} == {BOUNDARY_NS}
    assert not [e for e in _rows(records, RiskEventRow) if e.event_key.startswith("funding_unbooked:")]


def test_a_coin_with_no_rate_is_named_in_one_unbooked_event_and_the_other_still_books():
    records, _ = _funding_run([_rates(BTC_ID, D("0.0001"))])

    assert [r.instrument_id for r in _rows(records, FundingRow)] == [BTC_ID.value]
    (event,) = [e for e in _rows(records, RiskEventRow) if e.event_key.startswith("funding_unbooked:")]
    assert event.event_key == f"funding_unbooked:{PEPE_ID.value}:{BOUNDARY_NS}"


def test_no_rate_for_either_coin_raises_one_event_for_the_pass():
    records, _ = _funding_run([])

    (event,) = [e for e in _rows(records, RiskEventRow) if e.event_key.startswith("funding_unbooked:")]
    assert event.event_key == f"funding_unbooked:{BTC_ID.value},{PEPE_ID.value}:{BOUNDARY_NS}"
    assert event.details["rate_missing"] is True


def _position(position_id, instrument_id, qty):
    return SimpleNamespace(
        id=PositionId(position_id),
        instrument_id=instrument_id,
        signed_decimal_qty=lambda: qty,
        unrealized_pnl=lambda price: Money(1, USD),
    )


class _Cache:
    """The cache reads the liquidation helpers make: open positions, quotes, instruments."""

    def __init__(self, positions, quoted):
        self._positions, self._quoted = positions, quoted

    def positions_open(self):
        return self._positions

    def quote_tick(self, instrument_id):
        if instrument_id not in self._quoted:
            return None
        mid = Price(BTC_MID, 0)
        return SimpleNamespace(bid_price=mid, ask_price=mid)

    def instrument(self, instrument_id):
        return BTC_PERP if instrument_id == BTC_ID else PEPE_PERP


def test_a_coin_with_no_mid_is_left_out_of_the_other_coins_liquidation_inputs():
    btc, pepe = _position("BTC-1", BTC_ID, D("0.01")), _position("PEPE-1", PEPE_ID, D(1000))
    other_btc = _position("BTC-2", BTC_ID, D("0.02"))
    cache: Any = _Cache([btc, pepe, other_btc], quoted={BTC_ID})

    assert recording._open_notional_with_mid(cache, exclude=btc.id) == D("0.02") * BTC_MID
    assert recording._unrealized_with_mid(cache) == D(2)


# AC-19 and AC-16: r1 on two coins --------------------------------------------------------------


class _Script:
    """A scripted rule: ready from bar 3; at a bar in ``entries`` a long with its stop ``stop_pct`` below the close."""

    def __init__(self, entries, stop_pct):
        self.entries, self.stop_pct = set(entries), stop_pct
        self.n, self.close = 0, D(0)

    def update(self, bar, atr):
        self.n += 1
        self.close = bar.close

    def ready(self):
        return self.n >= 3

    def entry(self):
        if self.n not in self.entries:
            return None
        return EnterSignal(Side.LONG, self.close * (1 - self.stop_pct), "script_long")

    def consumed(self):
        self.entries.discard(self.n)

    def exit(self, trade):
        return None

    def on_flat(self):
        pass

    def values(self):
        return {"n": self.n}


class _ScriptedR1(DonchianScalpR1):
    def __init__(self, config, gate, blocks, record, rule):
        super().__init__(config, gate, blocks, record)
        self.rule = rule


STOP_PCT = D("0.01")


def _r1(run_id, instrument_id, blocks, records, entries, stop_pct=STOP_PCT, **risk):
    config = DonchianScalpR1Config(
        order_id_tag=instrument_id.symbol.value.removeprefix("PF_"),
        env="demo",
        run_id=str(run_id),
        instrument_id=str(instrument_id),
        bar_spec="5-MINUTE-LAST",
        entry_bars=2,
        stop_atr=D(1),
        ema_period=2,
        atr_period=2,
        time_stop_bars=4,  # a 20 minute hold crosses no funding time (no market context here)
        use_market_context=False,
        stale_quote_s=D(600),
        **_risk_fields(**risk),
    )
    return _ScriptedR1(config, PauseGate(), blocks, records.append, _Script(entries, stop_pct))


class _At(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Calls ``action`` once at ``at_ns``."""

    def __init__(self, at_ns, action):
        super().__init__(ActorConfig(component_id=f"At{at_ns}"))
        self._at_ns, self._action = at_ns, action

    def on_start(self):
        self.clock.set_time_alert_ns("at", self._at_ns, lambda event: self._action())


def _two_r1(pepe_entries, btc_entries, minutes=40, balance=10000, actions=(), pepe=True, **risk):
    engine = _engine(minutes, balance, pepe=pepe)
    records: list[WriteItem] = []
    run_id, blocks = uuid.uuid7(), EntryBlocks()
    engine.add_actor(_recording(run_id, records, blocks))
    pepe_r1 = _r1(run_id, PEPE_ID, blocks, records, pepe_entries, **risk)
    btc_r1 = _r1(run_id, BTC_ID, blocks, records, btc_entries, **risk)
    for strategy in (pepe_r1, btc_r1):
        engine.add_strategy(strategy)
    for at_ns, action in actions:
        engine.add_actor(_At(at_ns, lambda a=action: a(pepe_r1, btc_r1, blocks)))
    _run(engine)
    return engine, records, pepe_r1, btc_r1


def test_pepe_sizes_to_whole_steps_with_a_stop_on_its_tick():
    engine, _, pepe_r1, _ = _two_r1(pepe_entries=[4], btc_entries=[])

    (order, *_) = [o for o in engine.cache.orders(strategy_id=pepe_r1.id) if o.order_type.name == "MARKET"]
    stops = [o for o in engine.cache.orders(strategy_id=pepe_r1.id) if o.order_type.name == "STOP_MARKET"]
    qty = order.quantity.as_decimal()
    assert qty > 0
    assert qty % 1000 == 0
    assert qty * PEPE_MID <= D(1000)  # the per order cap
    (stop,) = stops
    trigger = stop.trigger_price.as_decimal()
    assert trigger % TICK == 0
    assert trigger < PEPE_MID


def test_pepe_with_too_small_an_account_refuses_with_zero_quantity_and_sends_no_order():
    # 1 USD equity risks 0.005 USD; with a 90 % stop that is under one step of 1000 PEPE.
    engine, records, pepe_r1, _ = _two_r1(pepe_entries=[4], btc_entries=[], balance=1, stop_pct=D("0.9"))

    (decision,) = [r for r in _rows(records, DecisionRow) if r.intent == Intent.ENTER]
    assert (decision.risk_verdict, decision.risk_reason) == (RiskVerdict.REJECTED, "zero_quantity")
    assert engine.cache.orders(strategy_id=pepe_r1.id) == []


def test_a_block_raised_for_btc_stops_a_pepe_entry():
    block_at = _ns(START) + 12 * 60 * SECOND_NS  # after bar 2, before PEPE's entry at bar 4

    engine, records, pepe_r1, _ = _two_r1(
        pepe_entries=[4],
        btc_entries=[],
        actions=[(block_at, lambda pepe, btc, blocks: blocks.add(BlockReason.STOP_NOT_ACCEPTED))],
    )

    (decision,) = [r for r in _rows(records, DecisionRow) if r.intent == Intent.ENTER]
    assert (decision.risk_verdict, decision.risk_reason) == (RiskVerdict.REJECTED, "blocked:stop_not_accepted")
    assert engine.cache.orders(strategy_id=pepe_r1.id) == []


def test_an_open_pepe_position_counts_in_btcs_exposure():
    engine, records, pepe_r1, _ = _two_r1(pepe_entries=[4], btc_entries=[6])

    pepe_decision, btc_decision = [r for r in _rows(records, DecisionRow) if r.intent == Intent.ENTER]
    assert D(str(pepe_decision.inputs["open_notional"])) == 0
    # The PEPE position has closed on its time stop by the end of the run, so take its entry size.
    (pepe_entry,) = [o for o in engine.cache.orders(strategy_id=pepe_r1.id) if o.side.name == "BUY"]
    assert D(str(btc_decision.inputs["open_notional"])) == pytest.approx(
        pepe_entry.quantity.as_decimal() * PEPE_MID, rel=D("0.001")
    )


def test_a_halt_flattens_an_open_pepe_position():
    halt_at = _ns(START) + 27 * 60 * SECOND_NS  # PEPE entered at bar 4 (20:00), still open

    engine, _, pepe_r1, _ = _two_r1(
        pepe_entries=[4], btc_entries=[], actions=[(halt_at, lambda pepe, btc, blocks: pepe.flatten(uuid.uuid7()))]
    )

    (position,) = engine.cache.positions(strategy_id=pepe_r1.id)
    assert position.is_closed
    closes = [o for o in engine.cache.orders(strategy_id=pepe_r1.id) if any("kill_switch" in t for t in o.tags or [])]
    assert closes
    assert all(o.is_closed for o in closes)

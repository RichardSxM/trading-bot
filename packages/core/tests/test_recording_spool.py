"""covers: spec 0006 AC-13 (the spool codec round trips every queued row type; a truncated last line is
skipped with a WARNING; anything else that is not a spool line refuses to decode).
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_core.recording.enums import (
    Env,
    Granularity,
    Intent,
    Liquidity,
    OrderEventKind,
    OrderOrigin,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionStatus,
    RiskEventKind,
    RiskVerdict,
    Severity,
    TimeInForce,
)
from trading_core.recording.rows import (
    AccountSnapshotRow,
    DecisionRow,
    FillRow,
    InstrumentRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    PositionSnapshotRow,
    RiskEventRow,
)
from trading_core.recording.spool import (
    ROW_TYPES,
    SpoolDecodeError,
    WriteCall,
    WriteItem,
    append,
    decode,
    encode,
    read,
)

NS = 1_791_116_102_123_456_789
RUN = uuid.uuid7()
DECISION = uuid.uuid7()
POSITION = uuid.uuid7()


def _samples():
    """One row per call, every optional field set, so the codec sees each field type."""
    return {
        WriteCall.UPSERT_INSTRUMENT: InstrumentRow(
            instrument_id="PF_XBTUSD.KRAKEN",
            venue="KRAKEN",
            symbol="PF_XBTUSD",
            base_currency="BTC",
            quote_currency="USD",
            settle_currency="USD",
            tick_size=Decimal("1"),
            lot_size=Decimal("0.0001"),
            multiplier=Decimal("1"),
            is_inverse=False,
        ),
        WriteCall.RECORD_DECISION: DecisionRow(
            id=DECISION,
            env=Env.DEMO,
            run_id=RUN,
            instrument_id="PF_XBTUSD.KRAKEN",
            intent=Intent.ENTER,
            side=OrderSide.BUY,
            reason="timed_entry",
            inputs={"mid": Decimal("85150.5"), "quote_age_ms": 120, "nested": {"a": [1, "b"]}},
            risk_verdict=RiskVerdict.REJECTED,
            risk_reason="notional_cap",
            decided_ns=NS,
        ),
        WriteCall.UPSERT_ORDER: OrderRow(
            env=Env.DEMO,
            run_id=RUN,
            decision_id=DECISION,
            origin=OrderOrigin.STRATEGY,
            client_order_id="O-2",
            venue_order_id="KRAKEN-1",
            parent_client_order_id="O-1",
            order_list_id="OL-1",
            instrument_id="PF_XBTUSD.KRAKEN",
            side=OrderSide.SELL,
            order_type=OrderType.STOP_MARKET,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("0.0011"),
            price=Decimal("85000"),
            trigger_price=Decimal("84895"),
            reduce_only=True,
            post_only=False,
            status=OrderStatus.ACCEPTED,
            filled_qty=Decimal("0.0000"),
            avg_px=Decimal("85151.25"),
            last_event_ns=NS,
        ),
        WriteCall.RECORD_ORDER_EVENT: OrderEventRow(
            env=Env.DEMO,
            run_id=RUN,
            client_order_id="O-2",
            event_id=uuid.uuid4(),
            kind=OrderEventKind.ORDER_ACCEPTED,
            details={"venue_order_id": "KRAKEN-1"},
            occurred_ns=NS,
        ),
        WriteCall.RECORD_FILL: FillRow(
            env=Env.DEMO,
            run_id=RUN,
            client_order_id="O-1",
            position_id=POSITION,
            instrument_id="PF_XBTUSD.KRAKEN",
            trade_id="T-1",
            side=OrderSide.BUY,
            quantity=Decimal("0.0011"),
            price=Decimal("85151"),
            fee=Decimal("0.05"),
            fee_currency="USD",
            liquidity=Liquidity.TAKER,
            filled_ns=NS,
        ),
        WriteCall.UPSERT_POSITION: PositionRow(
            id=POSITION,
            env=Env.DEMO,
            run_id=RUN,
            instrument_id="PF_XBTUSD.KRAKEN",
            position_ref="PF_XBTUSD.KRAKEN-PlaceholderTimed-000",
            side=PositionSide.FLAT,
            status=PositionStatus.CLOSED,
            quantity=Decimal("0"),
            peak_qty=Decimal("0.0011"),
            avg_entry_px=Decimal("85151"),
            avg_exit_px=Decimal("85249"),
            realized_pnl=Decimal("-0.12"),
            fees_total=Decimal("0.10"),
            funding_total=Decimal("0"),
            currency="USD",
            opened_ns=NS,
            closed_at=datetime(2026, 10, 4, 12, 25, 2, 123456, tzinfo=UTC),
            last_event_ns=NS + 1,
        ),
        WriteCall.RECORD_POSITION_SNAPSHOT: PositionSnapshotRow(
            env=Env.DEMO,
            position_id=POSITION,
            quantity=Decimal("-0.0011"),
            mark_px=Decimal("85200.5"),
            unrealized_pnl=Decimal("-0.05"),
            liquidation_px=Decimal("42000"),
            currency="USD",
            granularity=Granularity.RAW,
            captured_ns=NS,
        ),
        WriteCall.RECORD_ACCOUNT_SNAPSHOT: AccountSnapshotRow(
            env=Env.DEMO,
            run_id=RUN,
            account_ref="KRAKEN-001",
            balance_total=Decimal("999.90"),
            balance_free=Decimal("952.40"),
            margin_used=Decimal("47.50"),
            currency="USD",
            captured_ns=NS,
        ),
        WriteCall.RECORD_RISK_EVENT: RiskEventRow(
            env=Env.DEMO,
            event_key="stop_not_accepted:O-2",
            kind=RiskEventKind.ORDER_REJECTED,
            severity=Severity.CRITICAL,
            message="stop rejected; closing the position",
            details={"client_order_id": "O-2"},
            run_id=RUN,
            decision_id=DECISION,
            order_id=uuid.uuid7(),
            occurred_ns=NS,
        ),
    }


def test_the_samples_cover_every_call():
    assert set(_samples()) == set(ROW_TYPES)


@pytest.mark.parametrize("call", list(WriteCall))
def test_every_row_type_round_trips(call):
    item = WriteItem(call, _samples()[call])

    line = encode(item)

    assert "\n" not in line
    assert decode(line) == item


def test_a_file_round_trips_in_order(tmp_path):
    path = tmp_path / "run.jsonl"
    items = [WriteItem(call, row) for call, row in _samples().items()]

    append(path, items[:4])
    append(path, items[4:])

    assert list(read(path)) == items


def test_a_truncated_last_line_is_skipped_with_a_warning(tmp_path, caplog):
    path = tmp_path / "run.jsonl"
    first, second = (WriteItem(call, row) for call, row in list(_samples().items())[:2])
    append(path, [first])
    with path.open("a", encoding="utf-8") as file:
        file.write(encode(second)[:40])  # a crash mid write leaves no newline

    with caplog.at_level(logging.WARNING, logger="trading_core.recording.spool"):
        assert list(read(path)) == [first]

    assert [r.fields["code"] for r in caplog.records] == ["spool_truncated_line"]


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"call": "drop_table", "row_type": "FillRow", "row": {}}',
        '{"call": "record_fill", "row_type": "Secret", "row": {}}',
        '{"call": "record_fill", "row_type": "FillRow", "row": {"fee": "abc"}}',
        # the call and the row type disagree
        encode(WriteItem(WriteCall.RECORD_FILL, _samples()[WriteCall.RECORD_FILL])).replace(
            '"record_fill"', '"upsert_order"'
        ),
    ],
)
def test_anything_but_a_spool_line_refuses_to_decode(line):
    with pytest.raises(SpoolDecodeError):
        decode(line)

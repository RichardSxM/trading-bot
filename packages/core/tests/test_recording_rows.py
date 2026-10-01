"""covers: spec 0002 row values (AC-9 exact Decimal and UTC times, canonical JSON, version hash)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid7

import pytest

from trading_core.recording.enums import Env, Intent, Liquidity, OrderSide, RiskVerdict, RunMode
from trading_core.recording.rows import (
    DecisionRow,
    FillRow,
    InstrumentRow,
    JsonObject,
    RunRow,
    canonical_json,
    column_values,
    normalize_json,
    ns_to_utc,
    version_hash,
)


def _decision(**overrides):
    base = DecisionRow(
        env=Env.DEMO,
        run_id=uuid7(),
        instrument_id="PF_XBTUSD.KRAKEN",
        intent=Intent.ENTER,
        reason="breakout",
        inputs={"px": Decimal("98765.4321")},
        risk_verdict=RiskVerdict.APPROVED,
        decided_ns=1_727_000_000_123_456_789,
    )
    return replace(base, id=uuid7(), **overrides)


def test_ns_to_utc_is_aware_utc_truncated_to_microseconds():
    at = ns_to_utc(1_727_000_000_123_456_789)

    assert at == datetime(2024, 9, 22, 10, 13, 20, 123456, tzinfo=UTC)


def test_at_fields_are_derived_from_ns():
    row = _decision()

    assert row.decided_at == ns_to_utc(row.decided_ns)


def test_ids_default_to_time_ordered_uuid7():
    first, second = _decision(), _decision()

    assert first.id.version == 7
    assert first.id < second.id


def test_a_float_is_refused():
    with pytest.raises(TypeError, match="float"):
        InstrumentRow(
            instrument_id="PF_XBTUSD.KRAKEN",
            venue="KRAKEN",
            symbol="PF_XBTUSD",
            base_currency="BTC",
            quote_currency="USD",
            settle_currency="USD",
            tick_size=0.5,  # type: ignore[arg-type]
            lot_size=Decimal("0.0001"),
            multiplier=Decimal(1),
            is_inverse=False,
        )


def test_a_non_finite_decimal_is_refused():
    with pytest.raises(ValueError, match="finite"):
        _decision(inputs={"px": Decimal("NaN")})


@pytest.mark.parametrize(
    "started_at",
    [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))],  # noqa: DTZ001
)
def test_a_naive_or_non_utc_datetime_is_refused(started_at):
    with pytest.raises(ValueError, match="UTC"):
        RunRow(
            env=Env.DEMO,
            mode=RunMode.PAPER,
            strategy_version_id=uuid7(),
            trader_id="TRADER-DEMO",
            run_config={},
            started_at=started_at,
        )


def test_json_fields_are_normalized_like_jsonb_returns_them():
    row = _decision(inputs={"b": Decimal("0.00000001"), "a": [Decimal(1), 2, None]})

    assert row.inputs == {"a": ["1", 2, None], "b": "0.00000001"}


def test_canonical_json_sorts_keys_and_refuses_floats():
    assert canonical_json({"b": 1, "a": Decimal("1.50")}) == '{"a":"1.50","b":1}'
    with pytest.raises(TypeError, match="float"):
        canonical_json({"a": 1.5})  # type: ignore[dict-item]


def test_version_hash_is_stable_and_depends_on_commit_and_config():
    config: JsonObject = {"size": Decimal("0.01"), "window": 20}

    assert version_hash("abc", config) == version_hash("abc", {"window": 20, "size": Decimal("0.01")})
    assert version_hash("abc", config) != version_hash("abd", config)
    assert version_hash("abc", config) != version_hash("abc", {"size": Decimal("0.02"), "window": 20})
    assert len(version_hash("abc", config)) == 64


def test_normalize_json_round_trips_decimals_as_strings():
    assert normalize_json({"x": Decimal("98765.4321")}) == {"x": "98765.4321"}


def test_column_values_leave_out_lookup_only_fields():
    fill = FillRow(
        env=Env.DEMO,
        run_id=uuid7(),
        client_order_id="O-1",
        instrument_id="PF_XBTUSD.KRAKEN",
        trade_id="T-1",
        side=OrderSide.BUY,
        quantity=Decimal("0.001"),
        price=Decimal(60000),
        fee=Decimal("0.03"),
        fee_currency="USD",
        liquidity=Liquidity.TAKER,
        filled_ns=1,
    )

    values = column_values(fill)

    assert "client_order_id" not in values
    assert values["filled_at"] == ns_to_utc(1)

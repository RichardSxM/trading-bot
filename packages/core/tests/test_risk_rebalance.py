"""The rebalance check (spec 0030 AC-5)."""

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_core.recording.enums import OrderSide
from trading_core.risk.check import EXPOSURE_CAP, LEVERAGE_CAP, NOTIONAL_CAP, RiskLimits
from trading_core.risk.rebalance import (
    LIQ_DISTANCE,
    STALE_QUOTE,
    InvalidRebalanceInput,
    RebalanceRequest,
    check_rebalance,
)

LIMITS = RiskLimits(
    max_order_notional=Decimal(1000),
    max_leverage=Decimal("2.5"),
    max_total_exposure=Decimal(5000),
    risk_per_trade=Decimal("0.01"),
    maintenance_margin_rate=Decimal("0.01"),
    min_liq_distance_pct=Decimal(5),
    liq_stop_multiple=Decimal(2),
    stop_slippage_bps=Decimal(10),
)

REQUEST = RebalanceRequest(
    side=OrderSide.BUY,
    quantity=Decimal("0.005"),
    mid=Decimal(100_000),
    quote_age_s=Decimal(1),
    equity=Decimal(1000),
    book_exposure=Decimal(0),
    current_qty=Decimal(0),
    size_step=Decimal("0.0001"),
    instrument_maintenance_rate=Decimal(0),
    stale_quote_s=Decimal(10),
)


def test_a_small_order_is_approved_as_is():
    verdict = check_rebalance(REQUEST, LIMITS)
    assert verdict.approved
    assert verdict.quantity == Decimal("0.005")
    assert verdict.clips == ()
    assert verdict.liq_px is None  # a long smaller than equity has no liquidation price


def test_a_stale_quote_refuses():
    verdict = check_rebalance(replace(REQUEST, quote_age_s=Decimal(11)), LIMITS)
    assert not verdict.approved
    assert verdict.reason == STALE_QUOTE


def test_a_grow_over_the_order_cap_is_clipped():
    verdict = check_rebalance(replace(REQUEST, quantity=Decimal("0.02")), LIMITS)
    assert verdict.approved
    assert verdict.quantity == Decimal("0.0100")
    assert verdict.clips == (NOTIONAL_CAP,)


def test_the_book_exposure_cap_clips():
    request = replace(REQUEST, equity=Decimal(10_000), book_exposure=Decimal(4500))
    verdict = check_rebalance(replace(request, quantity=Decimal("0.008")), LIMITS)
    assert verdict.quantity == Decimal("0.0050")
    assert verdict.clips == (EXPOSURE_CAP,)


def test_the_leverage_cap_clips():
    request = replace(REQUEST, book_exposure=Decimal(2200))
    verdict = check_rebalance(request, LIMITS)
    assert verdict.quantity == Decimal("0.0030")
    assert verdict.clips == (LEVERAGE_CAP,)


def test_a_cap_that_leaves_nothing_refuses_with_its_name():
    verdict = check_rebalance(replace(REQUEST, book_exposure=Decimal(2500)), LIMITS)
    assert not verdict.approved
    assert verdict.reason == LEVERAGE_CAP


def test_a_liquidation_too_close_refuses():
    limits = replace(
        LIMITS,
        min_liq_distance_pct=Decimal(25),
        max_leverage=Decimal(10),
        max_order_notional=Decimal(100_000),
        max_total_exposure=Decimal(100_000),
    )
    verdict = check_rebalance(replace(REQUEST, quantity=Decimal("0.05")), limits)
    assert not verdict.approved
    assert verdict.reason == LIQ_DISTANCE


def test_a_short_grow_counts_the_current_short():
    request = replace(REQUEST, side=OrderSide.SELL, current_qty=Decimal("-0.002"), book_exposure=Decimal(200))
    verdict = check_rebalance(request, LIMITS)
    assert verdict.approved
    assert verdict.liq_px is not None
    assert verdict.liq_px > REQUEST.mid


@pytest.mark.parametrize(
    "change",
    [{"mid": Decimal(0)}, {"equity": Decimal(0)}, {"size_step": Decimal(0)}, {"quantity": Decimal(0)}],
)
def test_unjudgeable_input_raises(change):
    with pytest.raises(InvalidRebalanceInput):
        check_rebalance(replace(REQUEST, **change), LIMITS)

"""covers: spec 0006 AC-4 and AC-5; spec 0011 AC-1 to AC-3, AC-5 to AC-7, AC-9 to AC-12, AC-15.

The base entry: a long at 100 000 with a 0.3 % stop, 1000 USD equity. Its loss per unit is
300 (distance) + 49.85 (5 bps past the 99 700 stop) + 99.85 (0.05 % taker on both legs) = 449.70,
so the risk size is 1000 * 0.5 % ÷ 449.70 = 0.0111 and the 250 USD order cap (0.0025) binds.
"""

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_core.recording.enums import OrderSide, Severity
from trading_core.risk.check import EntryRequest, InvalidRiskInput, RiskLimits, check_entry, refusal_severity
from trading_core.risk.funding import funding_periods
from trading_core.risk.liquidation import liquidation_price, maintenance_rate
from trading_core.risk.sizing import entry_quantity, floor_to_step

HOUR_NS = 3_600 * 1_000_000_000
MIN_NS = 60 * 1_000_000_000

LIMITS = RiskLimits(
    max_order_notional=Decimal(250),
    max_leverage=Decimal(2),
    max_total_exposure=Decimal(500),
    risk_per_trade=Decimal("0.005"),
    maintenance_margin_rate=Decimal("0.01"),
    min_liq_distance_pct=Decimal(10),
    liq_stop_multiple=Decimal(3),
    stop_slippage_bps=Decimal(5),
)
# Caps far away, so the risk size binds.
WIDE = replace(LIMITS, max_order_notional=Decimal(5000), max_total_exposure=Decimal(5000))

BASE = EntryRequest(
    side=OrderSide.BUY,
    mid=Decimal(100_000),
    equity=Decimal(1000),
    open_notional=Decimal(0),
    size_step=Decimal("0.0001"),
    taker_fee=Decimal("0.0005"),
    instrument_maintenance_rate=Decimal(0),
    funding_periods=0,
    stop_px=Decimal(99_700),
    has_position=False,
    paused=False,
    halted=False,
    blocks=(),
)
SHORT = replace(BASE, side=OrderSide.SELL, stop_px=Decimal(100_300))


def request(**changes):
    return replace(BASE, **changes)


def fixed(quantity, **changes):
    return request(stop_px=None, fixed_quantity=Decimal(quantity), **changes)


# Sizing (AC-1, AC-2) -----------------------------------------------------------------------


def test_the_risk_size_binds_when_the_caps_are_far():
    verdict = check_entry(BASE, WIDE)
    assert verdict.approved
    assert verdict.figures.loss_per_unit == Decimal("449.70")
    assert (verdict.quantity, verdict.limited_by) == (Decimal("0.0111"), "risk")
    assert verdict.figures.risk_quantity == Decimal("0.0111")


def test_the_order_cap_clips_the_risk_size():
    verdict = check_entry(BASE, LIMITS)
    assert verdict.approved
    assert (verdict.quantity, verdict.limited_by) == (Decimal("0.0025"), "notional_cap")
    assert verdict.figures.risk_quantity == Decimal("0.0111")


def test_the_exposure_cap_counts_what_is_already_open():
    verdict = check_entry(request(open_notional=Decimal(300)), LIMITS)
    assert (verdict.quantity, verdict.limited_by) == (Decimal("0.0020"), "exposure_cap")


def test_the_leverage_cap_counts_equity_and_what_is_open():
    verdict = check_entry(request(equity=Decimal(1000), open_notional=Decimal(1850)), WIDE)
    assert (verdict.quantity, verdict.limited_by) == (Decimal("0.0015"), "leverage_cap")


def test_a_tie_goes_to_the_cap():
    verdict = check_entry(BASE, replace(WIDE, max_order_notional=Decimal(1110)))
    assert (verdict.quantity, verdict.limited_by) == (Decimal("0.0111"), "notional_cap")


def test_a_short_pays_the_same_terms_mirrored():
    verdict = check_entry(SHORT, WIDE)
    assert verdict.figures.loss_per_unit == Decimal("450.30")  # 300 + 50.15 + 100.15


@pytest.mark.parametrize(
    ("changes", "limits", "reason"),
    [
        ({}, replace(WIDE, risk_per_trade=Decimal("0.00001")), "zero_quantity"),
        ({"open_notional": Decimal(500)}, LIMITS, "exposure_cap"),
        ({"open_notional": Decimal(600)}, LIMITS, "exposure_cap"),  # headroom below zero counts as zero
        ({"equity": Decimal(100), "open_notional": Decimal(200)}, LIMITS, "leverage_cap"),
    ],
)
def test_no_room_refuses_with_what_bound(changes, limits, reason):
    verdict = check_entry(request(**changes), limits)
    assert (verdict.approved, verdict.reason, verdict.quantity) == (False, reason, Decimal("0.0000"))


# Fixed quantity callers (AC-3) ------------------------------------------------------------


def test_a_fixed_quantity_is_not_sized():
    verdict = check_entry(fixed("0.0012"), LIMITS)
    assert (verdict.approved, verdict.quantity, verdict.limited_by) == (True, Decimal("0.0012"), None)


@pytest.mark.parametrize(
    ("quantity", "changes", "reason"),
    [
        ("0", {}, "zero_quantity"),
        ("0.003", {}, "notional_cap"),  # 300 USD
        ("0.002", {"open_notional": Decimal(400)}, "exposure_cap"),  # 600 USD open after
        ("0.002", {"equity": Decimal(90)}, "leverage_cap"),  # 200 USD on 90 USD
        ("0.004", {"open_notional": Decimal(400), "equity": Decimal(10)}, "notional_cap"),  # all three: the first
    ],
)
def test_a_fixed_quantity_over_a_cap_is_refused(quantity, changes, reason):
    assert check_entry(fixed(quantity, **changes), LIMITS).reason == reason


def test_exactly_at_the_caps_is_allowed():
    assert check_entry(fixed("0.0025"), LIMITS).approved  # exactly 250 USD
    assert check_entry(fixed("0.0025", open_notional=Decimal(250)), LIMITS).approved  # exactly 500 open


# Liquidation (AC-5 to AC-7) ---------------------------------------------------------------


def _liq(side, quantity, equity=1000, other=0, fee=0, rate="0.01"):
    return liquidation_price(
        side, Decimal(quantity), Decimal(100_000), Decimal(equity), Decimal(other), Decimal(fee), Decimal(rate)
    )


def test_a_long_at_2x_liquidates_near_half_the_entry():
    # (2000 - 1000) ÷ (0.02 * 0.99)
    assert _liq(OrderSide.BUY, "0.02").quantize(Decimal("0.01")) == Decimal("50505.05")


def test_a_short_at_2x_liquidates_near_one_and_a_half_times_the_entry():
    # (1000 + 2000) ÷ (0.02 * 1.01)
    assert _liq(OrderSide.SELL, "0.02").quantize(Decimal("0.01")) == Decimal("148514.85")


def test_the_fee_and_the_other_notional_move_a_long_closer():
    # (2000 + 1 + 0.01 * 500 - 1000) ÷ (0.02 * 0.99)
    assert _liq(OrderSide.BUY, "0.02", other=500, fee=1).quantize(Decimal("0.01")) == Decimal("50808.08")


def test_a_long_at_1x_has_no_liquidation_price():
    assert _liq(OrderSide.BUY, "0.01") is None


@pytest.mark.parametrize(("quantity", "rate"), [("0", "0.01"), ("0.01", "0"), ("0.01", "1")])
def test_the_estimate_refuses_bad_inputs(quantity, rate):
    with pytest.raises(ValueError, match="must be"):
        _liq(OrderSide.BUY, quantity, rate=rate)


def test_the_larger_maintenance_rate_wins():
    assert maintenance_rate(Decimal("0.02"), Decimal("0.01")) == Decimal("0.02")
    assert maintenance_rate(Decimal(0), Decimal("0.01")) == Decimal("0.01")
    verdict = check_entry(request(instrument_maintenance_rate=Decimal("0.02")), LIMITS)
    assert verdict.figures.maintenance_rate == Decimal("0.02")


# A 10 USD stop, 2 % risk, and 20x caps size a 12.5x long: its liquidation is 7 % away.
HIGH = replace(
    LIMITS,
    max_order_notional=Decimal(20_000),
    max_total_exposure=Decimal(20_000),
    max_leverage=Decimal(20),
    risk_per_trade=Decimal("0.02"),
)
# 9000 USD on 1000 equity with a 5 % stop: liquidation 10.2 % away, inside 3 stop distances.
NEAR = replace(LIMITS, max_order_notional=Decimal(10_000), max_total_exposure=Decimal(10_000), max_leverage=Decimal(10))


def test_a_liquidation_too_close_is_refused():
    verdict = check_entry(request(stop_px=Decimal(99_990)), HIGH)
    assert verdict.reason == "liq_too_close"
    assert verdict.figures.liq_distance_pct is not None
    assert verdict.figures.liq_distance_pct < 10


def test_a_liquidation_inside_the_stop_buffer_is_refused():
    verdict = check_entry(request(stop_px=Decimal(95_000), fixed_quantity=Decimal("0.09")), NEAR)
    assert verdict.reason == "liq_inside_stop"
    assert verdict.figures.liq_distance_pct is not None
    assert verdict.figures.liq_distance_pct > 10


def test_no_stop_skips_the_buffer_rule():
    assert check_entry(fixed("0.09"), NEAR).approved


def test_too_close_comes_before_inside_the_stop():
    verdict = check_entry(request(stop_px=Decimal(95_000), fixed_quantity=Decimal("0.095")), NEAR)
    assert verdict.reason == "liq_too_close"


# Funding (AC-1 funding term, AC-9, AC-10) -------------------------------------------------


def test_a_long_across_a_boundary_pays_a_positive_rate():
    verdict = check_entry(request(funding_periods=1, funding_rate=Decimal("0.0001")), WIDE)
    assert verdict.figures.loss_per_unit == Decimal("459.70")  # + 1 * 0.0001 * 100 000


def test_a_short_earns_a_positive_rate_and_counts_it_as_zero():
    verdict = check_entry(replace(SHORT, funding_periods=1, funding_rate=Decimal("0.0001")), WIDE)
    assert verdict.figures.loss_per_unit == Decimal("450.30")


def test_a_short_pays_a_negative_rate():
    verdict = check_entry(replace(SHORT, funding_periods=2, funding_rate=Decimal("-0.0001")), WIDE)
    assert verdict.figures.loss_per_unit == Decimal("470.30")  # + 2 * 0.0001 * 100 000


def test_a_stale_rate_refuses_only_when_a_boundary_is_inside_the_hold():
    assert check_entry(request(funding_periods=1, funding_rate=None), LIMITS).reason == "funding_stale"
    assert check_entry(request(funding_periods=0, funding_rate=None), LIMITS).approved


@pytest.mark.parametrize(
    ("now_ns", "hold_ns", "periods"),
    [
        (10 * HOUR_NS + 50 * MIN_NS, 10 * MIN_NS, 1),  # ends exactly on the boundary: counted
        (10 * HOUR_NS + 45 * MIN_NS, 10 * MIN_NS, 0),
        (10 * HOUR_NS, 10 * MIN_NS, 0),  # starts on the boundary: not counted
        (10 * HOUR_NS + 30 * MIN_NS, 2 * HOUR_NS, 2),
        (10 * HOUR_NS, None, 1),
    ],
)
def test_funding_periods_count_the_boundaries_inside_the_hold(now_ns, hold_ns, periods):
    assert funding_periods(now_ns, hold_ns) == periods


# Order and record (AC-11, AC-12) ----------------------------------------------------------


def test_refusals_come_in_the_spec_order():
    everything = request(halted=True, paused=True, blocks=("stale_feed",), has_position=True, funding_periods=1)
    order = []
    for undo in ({}, {"halted": False}, {"paused": False}, {"blocks": ()}, {"has_position": False}):
        everything = replace(everything, **undo)
        order.append(check_entry(everything, replace(WIDE, risk_per_trade=Decimal("0.00001"))).reason)
    order.append(
        check_entry(replace(everything, funding_periods=0), replace(WIDE, risk_per_trade=Decimal("1e-5"))).reason
    )
    assert order == ["halted", "paused", "blocked:stale_feed", "position_open", "funding_stale", "zero_quantity"]


def test_the_first_block_in_sorted_order_is_named():
    verdict = check_entry(request(blocks={"stale_feed", "exit_failed", "recording_unhealthy"}), LIMITS)
    assert verdict.reason == "blocked:exit_failed"


def test_every_figure_is_recorded():
    figures = check_entry(BASE, LIMITS).figures.as_inputs()
    assert set(figures) == {
        "equity",
        "entry_px",
        "stop_px",
        "open_notional",
        "taker_fee",
        "maintenance_rate",
        "funding_rate",
        "funding_periods",
        "loss_per_unit",
        "risk_quantity",
        "quantity",
        "limited_by",
        "liq_px",
        "liq_distance_pct",
    }
    assert figures["entry_px"] == Decimal(100_000)
    assert figures["liq_px"] is None  # 250 USD on 1000 USD


def test_a_fixed_quantity_records_no_stop_or_risk_figures():
    figures = check_entry(fixed("0.0012"), LIMITS).figures
    assert (figures.stop_px, figures.loss_per_unit, figures.risk_quantity) == (None, None, None)


def test_a_refusal_still_records_its_figures():
    figures = check_entry(request(halted=True), LIMITS).figures
    assert figures.quantity == Decimal("0.0025")


# Inputs that cannot be judged (AC-15) -----------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"mid": Decimal(0)},
        {"mid": Decimal(-1)},
        {"equity": Decimal(0)},
        {"stop_px": Decimal(100_100)},  # above the mid for a long
        {"stop_px": Decimal(100_000)},  # equal to the mid
        {"size_step": Decimal(0)},
        {"stop_px": None},  # neither a stop nor a fixed quantity
    ],
)
def test_unjudgeable_inputs_raise(changes):
    with pytest.raises(InvalidRiskInput):
        check_entry(request(**changes), LIMITS)


def test_a_short_stop_below_the_mid_raises():
    with pytest.raises(InvalidRiskInput):
        check_entry(replace(SHORT, stop_px=Decimal(99_900)), LIMITS)


@pytest.mark.parametrize(
    ("reason", "severity"),
    [
        ("halted", Severity.INFO),
        ("paused", Severity.INFO),
        ("blocked:exit_failed", Severity.INFO),
        ("position_open", Severity.INFO),
        ("funding_stale", Severity.INFO),
        ("zero_quantity", Severity.WARNING),
        ("notional_cap", Severity.WARNING),
        ("exposure_cap", Severity.WARNING),
        ("leverage_cap", Severity.WARNING),
        ("liq_too_close", Severity.WARNING),
        ("liq_inside_stop", Severity.WARNING),
    ],
)
def test_refusal_severity(reason, severity):
    assert refusal_severity(reason) == severity


# Rounding -------------------------------------------------------------------------------


def test_entry_quantity_rounds_down_to_the_size_step():
    assert entry_quantity(Decimal(100), Decimal(85250), Decimal("0.0001")) == Decimal("0.0011")


def test_entry_quantity_is_exact_on_a_whole_step():
    assert entry_quantity(Decimal(100), Decimal(50000), Decimal("0.0001")) == Decimal("0.0020")


def test_entry_quantity_is_zero_below_one_step():
    assert entry_quantity(Decimal(1), Decimal(85250), Decimal("0.0001")) == Decimal("0.0000")


@pytest.mark.parametrize(("notional", "mid", "step"), [(0, 1, 1), (1, 0, 1), (1, 1, 0), (-1, 1, 1)])
def test_entry_quantity_refuses_non_positive_inputs(notional, mid, step):
    with pytest.raises(ValueError, match="must be above zero"):
        entry_quantity(Decimal(notional), Decimal(mid), Decimal(step))


def test_floor_to_step_turns_a_negative_headroom_into_zero():
    assert floor_to_step(Decimal(-5), Decimal("0.0001")) == Decimal("0.0000")

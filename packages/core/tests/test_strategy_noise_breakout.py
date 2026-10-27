"""covers: spec 0020 AC-5 (``noise_breakout``'s rules on hand built 30 minute bars), 0020.1 *Tests*.

The time stop is the base's (test_node_bar_rule), and the config's 16 bar time stop and its refusals are
in test_node_noise_breakout; here the rule's side: no ``noise_trail`` while the trade's line is ``None``.
"""

from datetime import UTC, datetime
from decimal import Decimal

from trading_core.strategy.bar_rule import BarInput, EnterSignal, Side, TradeView
from trading_core.strategy.noise_breakout import NoiseBreakoutRule

D = Decimal
HALF_HOUR_NS = 1_800_000_000_000
DAY_NS = 48 * HALF_HOUR_NS
DAY0 = int(datetime(2026, 2, 1, tzinfo=UTC).timestamp()) * 10**9
ATR = D(5)
HALF = D("0.5")
ONE = D(1)


def _rule(lookback_days=14, band_mult=ONE, time_stop_bars=16):
    return NoiseBreakoutRule(
        HALF_HOUR_NS, lookback_days=lookback_days, band_mult=band_mult, time_stop_bars=time_stop_bars
    )


def _bar(day, slot, close, *, open_=None, high=None, low=None, volume=1, mirror=False):
    """The bar opening at ``slot`` of day ``day``; ``mirror`` reflects every price around 1000."""
    o, c = D(close if open_ is None else open_), D(close)
    h = max(o, c) + HALF if high is None else D(high)
    lo = min(o, c) - HALF if low is None else D(low)
    if mirror:
        o, c, h, lo = 2000 - o, 2000 - c, 2000 - lo, 2000 - h
    return BarInput(DAY0 + day * DAY_NS + (slot + 1) * HALF_HOUR_NS, o, h, lo, c, D(volume))


def _feed(rule, day, slot, close, **kwargs):
    rule.update(_bar(day, slot, close, **kwargs), ATR)


def _quiet_day(rule, day, *, skip=(), mirror=False):
    """Opens at 1000, every close at 1001: each slot moves 0.001 from the open, so bands sit at 999 and 1001."""
    for slot in range(48):
        if slot not in skip:
            _feed(rule, day, slot, 1001, open_=1000 if slot == 0 else 1001, mirror=mirror)


def _quiet_days(rule, days, **kwargs):
    for day in range(days):
        _quiet_day(rule, day, **kwargs)


def _flat_until(rule, day, last_slot, mirror=False):
    """Day ``day`` flat at 1000 from slot 0 to ``last_slot``; no signal on any of these bars."""
    for slot in range(last_slot + 1):
        _feed(rule, day, slot, 1000, mirror=mirror)
        assert rule.entry() is None


def test_quiet_days_never_signal_and_a_steady_rise_from_1000_utc_gives_a_long_stopped_at_long_line():
    rule = _rule()
    _quiet_days(rule, 14)
    _flat_until(rule, 14, 19)  # 00:00 to 10:00 UTC
    _feed(rule, 14, 20, 1002, open_=1000)
    assert rule.entry() == EnterSignal(Side.LONG, D(1001), "noise_long")
    values = rule.values()
    assert (values["day_open"], values["sigma"], values["upper"], values["lower"]) == (
        D(1000),
        D("0.001"),
        D(1001),
        D(999),
    )
    assert values["long_line"] == values["upper"] > values["vwap"]


def test_the_mirror_gives_a_short_stopped_at_short_line():
    rule = _rule()
    _quiet_days(rule, 14, mirror=True)
    _flat_until(rule, 14, 19, mirror=True)
    _feed(rule, 14, 20, 1002, open_=1000, mirror=True)
    assert rule.entry() == EnterSignal(Side.SHORT, D(999), "noise_short")


def test_a_close_above_the_upper_band_but_below_the_vwap_gives_no_signal():
    rule = _rule()
    _quiet_days(rule, 14)
    _feed(rule, 14, 0, 1000, high=1030, low=1000, volume=100)  # VWAP 1010
    _feed(rule, 14, 1, 1003)
    values = rule.values()
    assert values["upper"] < D(1003) < values["vwap"]
    assert values["long_line"] == values["vwap"]
    assert rule.entry() is None


def test_a_close_back_below_long_line_exits_with_noise_trail_and_the_next_close_above_it_signals_again():
    rule = _rule()
    _quiet_days(rule, 14)
    _flat_until(rule, 14, 19)
    _feed(rule, 14, 20, 1002, open_=1000)
    assert rule.entry() is not None
    _feed(rule, 14, 21, D("1000.5"), open_=1002)
    trade = TradeView(Side.LONG, D(1002), D(995), 1)
    assert rule.exit(trade) == "noise_trail"
    assert rule.values()["exit_line"] == D(1001)
    _feed(rule, 14, 22, 1003, open_=D("1000.5"))
    assert rule.entry() == EnterSignal(Side.LONG, D(1001), "noise_long")  # unlimited re-entry


def test_a_short_exits_on_a_close_back_above_short_line():
    rule = _rule()
    _quiet_days(rule, 14, mirror=True)
    _flat_until(rule, 14, 19, mirror=True)
    _feed(rule, 14, 20, 1002, open_=1000, mirror=True)
    _feed(rule, 14, 21, D("1000.5"), open_=1002, mirror=True)
    assert rule.exit(TradeView(Side.SHORT, D(998), D(1005), 1)) == "noise_trail"


def _long_late_on_day_14(rule, entry_slot):
    _quiet_days(rule, 14)
    _flat_until(rule, 14, entry_slot - 1)
    _feed(rule, 14, entry_slot, 1002, open_=1000)
    assert rule.entry() is not None


def test_a_long_held_past_midnight_trails_against_the_entry_days_frozen_line_not_the_new_days():
    rule = _rule()
    _long_late_on_day_14(rule, 45)  # 22:30 UTC
    _feed(rule, 14, 46, 1003, open_=1002)
    _feed(rule, 14, 47, 1003)
    assert rule.exit(TradeView(Side.LONG, D(1002), D(995), 2)) is None
    _feed(rule, 15, 0, 1005, open_=1010)  # above the frozen 1001, below the new day's upper band
    assert rule.values()["long_line"] > D(1005)
    assert rule.exit(TradeView(Side.LONG, D(1002), D(995), 3)) is None
    assert rule.values()["exit_line"] == D(1001)
    _feed(rule, 15, 1, 1000, open_=1005)
    assert rule.exit(TradeView(Side.LONG, D(1002), D(995), 4)) == "noise_trail"


def test_with_a_bar_missing_since_the_entry_the_entry_day_is_still_the_decision_bars_date():
    rule = _rule()
    _long_late_on_day_14(rule, 46)
    # slot 47 never came: the next bar is 00:00, one processed bar after the decision
    _feed(rule, 15, 0, 1005, open_=1010)
    assert rule.exit(TradeView(Side.LONG, D(1002), D(995), 1)) is None
    assert rule.values()["exit_line"] == D(1001)


def test_a_long_whose_entry_day_ended_with_no_line_gets_no_noise_trail_after_midnight():
    rule = _rule(lookback_days=2)
    _quiet_day(rule, 0)
    _quiet_day(rule, 1, skip={47})  # slot 47 has 1 day of history on day 2, so no line there
    _flat_until(rule, 2, 44)
    _feed(rule, 2, 45, 1002, open_=1000)
    assert rule.entry() is not None
    _feed(rule, 2, 46, 1003, open_=1002)
    _feed(rule, 2, 47, 1003)
    assert rule.values()["long_line"] is None
    _feed(rule, 3, 0, 900, open_=1000)  # far below every line
    for held in (3, 16):  # the base's time stop at 16 closes it, not the rule
        assert rule.exit(TradeView(Side.LONG, D(1002), D(995), held)) is None
    assert rule.values()["exit_line"] is None
    assert rule.ready()


def test_bars_held_past_the_oldest_remembered_bar_uses_the_oldest_date():
    rule = _rule(lookback_days=1, time_stop_bars=2)  # remembers 4 bars
    _quiet_day(rule, 0)
    _quiet_day(rule, 1)
    assert rule.exit(TradeView(Side.LONG, D(1002), D(995), 50)) is None  # day 1, the oldest in memory
    assert rule.values()["exit_line"] == rule.values()["long_line"]
    assert len(rule.days) == 4
    assert set(rule.day_lines) == set(rule.days)


def test_a_day_with_no_0000_bar_gives_no_signal_all_day_and_ready_stays_true():
    rule = _rule(lookback_days=1)
    _quiet_day(rule, 0)
    _quiet_day(rule, 1)
    assert rule.ready()
    for slot in range(1, 48):
        _feed(rule, 2, slot, 1100, open_=1100)
        assert rule.values()["day_open"] is None
        assert rule.entry() is None
        assert rule.ready()


def test_ready_turns_true_at_the_first_sigma_and_stays_true():
    rule = _rule(lookback_days=1)
    _quiet_day(rule, 0)
    assert not rule.ready()
    _feed(rule, 1, 0, 1001, open_=1000)
    assert rule.ready()
    _feed(rule, 2, 5, 1001)  # a day with no bands
    assert rule.values()["upper"] is None
    assert rule.ready()


def test_no_signal_before_the_slot_has_14_earlier_days():
    rule = _rule()
    _quiet_days(rule, 13)
    _flat_until(rule, 13, 19)
    _feed(rule, 13, 20, 1100, open_=1000)
    assert rule.entry() is None
    assert not rule.ready()


def test_zero_volume_bars_at_the_day_start_give_no_vwap_so_no_signal():
    rule = _rule(lookback_days=1)
    _quiet_day(rule, 0)
    _feed(rule, 1, 0, 1000, volume=0)
    _feed(rule, 1, 1, 1010, open_=1000, volume=0)
    assert rule.values()["vwap"] is None
    assert rule.values()["long_line"] is None
    assert rule.entry() is None
    _feed(rule, 1, 2, 1010, volume=1)  # the VWAP is now 1010, so long_line is 1010 and the close is not above it
    assert rule.values()["long_line"] == D(1010)
    assert rule.entry() is None


def test_values_hold_the_day_as_an_iso_date_and_every_line():
    rule = _rule(lookback_days=1)
    _quiet_day(rule, 0)
    assert set(rule.values()) == {
        "day",
        "slot",
        "day_open",
        "sigma",
        "upper",
        "lower",
        "vwap",
        "long_line",
        "short_line",
        "exit_line",
    }
    assert (rule.values()["day"], rule.values()["slot"]) == ("2026-02-01", 47)

"""covers: spec 0019 AC-6 (ema_pullback's rules on hand built 15 minute bars).

The time stop and the dropped trigger in a position are the base's (test_node_bar_rule); here the rule's
side of them: ``consumed`` disarms, and the config's 32 bar time stop is in test_node_ema_pullback.
"""

from decimal import Decimal

from trading_core.strategy.bar_rule import BarInput, Side, TradeView
from trading_core.strategy.ema_pullback import EmaPullbackRule

D = Decimal
ATR = D(10)
QUARTER_NS = 900_000_000_000
# Mirror prices around this level for the short cases.
MIRROR = D(3000)
TWO = D(2)


def _rule(rsi_period=7, rsi_high=50, target_r=TWO):
    return EmaPullbackRule(
        QUARTER_NS, fast=8, slow=21, rsi_period=rsi_period, rsi_low=30, rsi_high=rsi_high, arm_bars=6, target_r=target_r
    )


def _bar(spec):
    """A close, or (close, low, high); a bare close gets a low 5 under and a high 5 over it."""
    close, low, high = spec if isinstance(spec, tuple) else (spec, spec - 5, spec + 5)
    return D(close), D(low), D(high)


def _mirror(spec):
    close, low, high = _bar(spec)
    return MIRROR - close, MIRROR - high, MIRROR - low


def _feed(rule, specs, start=0):
    for i, spec in enumerate(specs, start):
        close, low, high = _bar(spec)
        rule.update(BarInput((i + 1) * QUARTER_NS, close, high, low, close, D(1)), ATR)


# 30 bars of a choppy up trend (+30, -20, closing at 1150): EMA(8) about 1143, EMA(21) about 1112, RSI(7) 56 to 63.
UP = [c + step for c in range(1000, 1150, 10) for step in (30, 10)]


def test_a_dip_to_the_fast_ema_then_a_close_back_above_gives_one_long_stopped_at_the_lower_of_dip_and_slow_ema():
    rule = _rule()
    _feed(rule, [*UP, 1140, 1130])
    assert rule.values()["armed"] == "long"  # 1130's low 1125 is under EMA(8) 1139.9, RSI(7) 49
    assert D(30) <= rule.values()["rsi"] <= D(50)
    assert rule.entry() is None  # 1130 closed under EMA(8)
    _feed(rule, [1150], len(UP) + 2)
    signal = rule.entry()
    assert signal is not None
    assert (signal.side, signal.reason) == (Side.LONG, "ema_pullback_long")
    values = rule.values()
    assert signal.structural_stop == min(values["pullback_extreme"], values["ema_slow"]) == values["ema_slow"]


def test_a_deep_dip_puts_the_stop_at_the_dip_low():
    rule = _rule()
    _feed(rule, [*UP, 1060, 1140])
    signal = rule.entry()
    assert signal is not None
    assert signal.structural_stop == D(1055)  # the dip bar's low, under EMA(21)


def test_a_dip_bar_that_closes_back_above_the_fast_ema_gives_the_long_on_that_same_bar():
    rule = _rule(rsi_period=3)
    _feed(rule, [*UP, (1148, 1130, 1152)])
    assert (rule.values()["armed"], rule.values()["armed_bars"]) == ("long", 1)
    signal = rule.entry()
    assert signal is not None
    assert signal.side == Side.LONG


def test_the_mirror_gives_a_short_stopped_at_the_higher_of_the_pop_and_the_slow_ema():
    rule = _rule()
    for i, spec in enumerate([*UP, 1140, 1130, 1150]):
        close, low, high = _mirror(spec)
        rule.update(BarInput((i + 1) * QUARTER_NS, close, high, low, close, D(1)), ATR)
    signal = rule.entry()
    assert signal is not None
    assert (signal.side, signal.reason) == (Side.SHORT, "ema_pullback_short")
    values = rule.values()
    assert values["trend"] == "down"
    assert signal.structural_stop == max(values["pullback_extreme"], values["ema_slow"])


def test_rsi_above_the_band_on_the_dip_bar_does_not_arm():
    rule = _rule()
    _feed(rule, [*UP, (1148, 1130, 1152)])  # the low reaches EMA(8), but RSI(7) is 55
    assert rule.values()["rsi"] > 50
    assert rule.values()["armed"] == "none"
    assert rule.entry() is None


def test_a_close_below_the_slow_ema_while_armed_disarms_and_a_later_close_above_the_fast_ema_gives_nothing():
    rule = _rule()
    _feed(rule, [*UP, 1140, 1130])
    assert rule.values()["armed"] == "long"
    _feed(rule, [1040], len(UP) + 2)  # far under EMA(21); RSI(7) under 30, so it cannot arm again
    assert rule.values()["armed"] == "none"
    _feed(rule, [1150], len(UP) + 3)
    assert rule.entry() is None


def test_an_arm_triggers_on_its_sixth_bar_but_not_on_its_seventh():
    sixth = _rule()
    _feed(sixth, [*UP, 1140, 1130, 1125, 1124, 1123, 1122, 1150])
    assert sixth.values()["armed_bars"] == 6
    assert sixth.entry() is not None

    seventh = _rule()
    _feed(seventh, [*UP, 1140, 1130, 1125, 1124, 1123, 1122, 1121, 1150])
    assert seventh.values()["armed"] == "none"
    assert seventh.entry() is None


def test_a_valid_dip_on_the_seventh_bar_of_a_stale_arm_arms_again_with_its_own_low():
    rule = _rule()
    _feed(rule, [*UP, 1140, 1130, 1125, 1124, 1123, 1122, 1121, (1120, 1110, 1125)])  # 1130 arms; 1121 is bar 6
    values = rule.values()
    assert (values["armed"], values["armed_bars"], values["pullback_extreme"]) == ("long", 1, D(1110))


def test_a_consumed_trigger_disarms_so_the_same_arm_never_fires_again():
    rule = _rule()
    _feed(rule, [*UP, 1140, 1130, 1150])
    assert rule.entry() is not None
    rule.consumed()  # what the base does with a trigger while in a position
    assert rule.values()["armed"] == "none"
    _feed(rule, [1160], len(UP) + 3)
    assert rule.entry() is None
    rule.on_flat()
    assert rule.entry() is None


def test_a_long_exits_with_target_at_the_first_close_at_2_r():
    rule = _rule()
    trade = TradeView(Side.LONG, D(1150), D(1120), 3)  # risk 30, so 2 R is 1210
    _feed(rule, [*UP, 1200])
    assert rule.exit(trade) is None
    _feed(rule, [1210], len(UP) + 1)
    assert rule.exit(trade) == "target"


def test_a_short_exits_with_target_at_the_first_close_at_1_5_r():
    rule = _rule(target_r=D("1.5"))
    trade = TradeView(Side.SHORT, D(1150), D(1170), 3)  # risk 20, so 1.5 R is 1120
    _feed(rule, [*UP, 1121])
    assert rule.exit(trade) is None
    _feed(rule, [1120], len(UP) + 1)
    assert rule.exit(trade) == "target"


def test_the_rule_is_ready_at_the_close_of_bar_21_and_not_before():
    rule = _rule()
    _feed(rule, UP[:20])
    assert not rule.ready()
    assert rule.values()["trend"] == "none"
    _feed(rule, UP[20:21], 20)
    assert rule.ready()


def test_the_values_carry_every_input_the_decision_records():
    rule = _rule()
    _feed(rule, [*UP, 1140, 1130])
    assert set(rule.values()) == {"ema_fast", "ema_slow", "rsi", "trend", "armed", "pullback_extreme", "armed_bars"}

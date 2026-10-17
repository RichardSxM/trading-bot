"""covers: spec 0014 AC-12 (buy_and_hold's size and attempt cap)."""

from decimal import Decimal

import pytest

from trading_core.strategy.buy_and_hold import EntryAttempts, hold_quantity

D = Decimal


def test_the_quantity_is_the_notional_at_the_mid_floored_to_the_step():
    assert hold_quantity(D(10000), D(60000), D("0.0001")) == D("0.1666")
    assert hold_quantity(D(1), D(60000), D("0.0001")) == D(0)


def test_attempts_give_up_once_at_the_cap_and_reset_after_a_close():
    attempts = EntryAttempts(2)
    seen = []
    for step in (attempts.refuse, attempts.refuse, attempts.reset):
        given_up = step()
        seen.append((given_up, attempts.abandoned))
    # The second refusal reaches the cap; a reset clears it.
    assert seen == [(False, False), (True, True), (None, False)]


def test_the_cap_must_allow_one_attempt():
    with pytest.raises(ValueError, match="at least 1"):
        EntryAttempts(0)

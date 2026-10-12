"""covers: spec 0009 AC-8, AC-9, AC-10 (the pure ContextBook)."""

from decimal import Decimal

import pytest

from trading_core.marketdata.context import (
    AggressorSide,
    ContextBook,
    ContextLookAheadError,
    ContextValue,
    Liquidation,
    LiquidationKind,
    MaxAges,
)
from trading_core.marketdata.hours import SECOND_NS

AGES = MaxAges.from_seconds(mark=30, index=30, open_interest=30, funding=7_200)
WINDOW = 60 * SECOND_NS
T = 1_000 * SECOND_NS


def book(*, liquidations_available=True):
    return ContextBook(AGES, WINDOW, liquidations_available=liquidations_available)


def liq(ts, *, side=AggressorSide.SELL, kind=LiquidationKind.LIQUIDATION, price="80000", size="0.5", uid=None):
    return Liquidation(uid or f"u{ts}", kind, side, Decimal(price), Decimal(size), ts)


def test_an_empty_book_knows_nothing():
    ctx = book().snapshot(T)

    assert (ctx.mark, ctx.index, ctx.basis, ctx.funding_rate, ctx.open_interest, ctx.next_funding_ns) == (None,) * 6
    assert ctx.liquidations.known is False
    assert ctx.liquidations.last is None


def test_funding_is_known_at_its_ts_init_with_its_next_funding_time():
    b = book()
    b.apply_funding(Decimal("0.000014034466666667"), T + 900 * SECOND_NS, T)

    ctx = b.snapshot(T + SECOND_NS)

    assert ctx.funding_rate == ContextValue(Decimal("0.000014034466666667"), T, False)
    assert ctx.next_funding_ns == T + 900 * SECOND_NS


def test_a_value_one_ns_past_its_max_age_is_stale():
    b = book()
    b.apply_mark(Decimal(83597), T)

    assert b.snapshot(T + 30 * SECOND_NS).mark.is_stale is False
    assert b.snapshot(T + 30 * SECOND_NS + 1).mark.is_stale is True


def test_an_older_update_is_ignored_and_an_equal_one_replaces():
    b = book()
    b.apply_mark(Decimal(2), T)
    b.apply_mark(Decimal(1), T - 1)
    assert b.snapshot(T).mark.value == Decimal(2)

    b.apply_mark(Decimal(3), T)
    assert b.snapshot(T).mark.value == Decimal(3)


def test_an_older_funding_update_keeps_the_next_funding_time():
    b = book()
    b.apply_funding(Decimal("0.1"), 5, T)
    b.apply_funding(Decimal("0.2"), 9, T - 1)

    ctx = b.snapshot(T)

    assert (ctx.funding_rate.value, ctx.next_funding_ns) == (Decimal("0.1"), 5)


def test_a_snapshot_before_an_applied_update_raises():
    b = book()
    b.apply_index(Decimal(1), T)

    with pytest.raises(ContextLookAheadError) as exc:
        b.snapshot(T - 1)
    assert (exc.value.now_ns, exc.value.latest_ns) == (T - 1, T)


def test_basis_is_mark_minus_index_over_index_known_at_the_later_one():
    b = book()
    b.apply_mark(Decimal(83597), T)
    b.apply_index(Decimal(83587), T + SECOND_NS)

    basis = b.snapshot(T + SECOND_NS).basis

    assert basis.value == Decimal(10) / Decimal(83587)
    assert basis.known_ns == T + SECOND_NS
    assert basis.is_stale is False


def test_basis_is_stale_when_either_side_is():
    b = book()
    b.apply_mark(Decimal(83597), T + 40 * SECOND_NS)
    b.apply_index(Decimal(83587), T)

    ctx = b.snapshot(T + 40 * SECOND_NS)

    assert (ctx.mark.is_stale, ctx.index.is_stale, ctx.basis.is_stale) == (False, True, True)


def test_basis_ignores_the_thread_decimal_context():
    import decimal

    b = book()
    b.apply_mark(Decimal(2), T)
    b.apply_index(Decimal(3), T)
    with decimal.localcontext(prec=3):
        value = b.snapshot(T).basis.value

    assert value == Decimal("-0.3333333333333333333333333333")


def test_liquidation_sums_by_side_in_exact_usd():
    b = book()
    b.apply_open_interest(Decimal("2423.1378"), T)
    b.apply_liquidation(liq(T, side=AggressorSide.SELL, price="83597.5", size="0.1234"))
    b.apply_liquidation(liq(T + 1, side=AggressorSide.BUY, price="83600", size="2"))
    b.apply_liquidation(liq(T + 2, side=AggressorSide.SELL, price="83590", size="0.0001"))

    w = b.snapshot(T + 2).liquidations

    assert w.known is True
    assert (w.sold_size, w.sold_notional) == (
        Decimal("0.1235"),
        Decimal("83597.5") * Decimal("0.1234") + Decimal("83590") * Decimal("0.0001"),
    )
    assert (w.bought_size, w.bought_notional) == (Decimal(2), Decimal(167200))
    assert w.last.ts_init == T + 2


def test_a_liquidation_exactly_window_old_is_out_and_one_ns_newer_is_in():
    b = book()
    b.apply_liquidation(liq(T, size="1"))
    b.apply_liquidation(liq(T + 1, size="2"))
    b.apply_open_interest(Decimal(1), T + WINDOW)

    w = b.snapshot(T + WINDOW).liquidations

    assert w.sold_size == Decimal(2)


def test_termination_and_block_are_not_summed():
    b = book()
    b.apply_open_interest(Decimal(1), T)
    b.apply_liquidation(liq(T, kind=LiquidationKind.TERMINATION))
    b.apply_liquidation(liq(T, kind=LiquidationKind.BLOCK))

    w = b.snapshot(T).liquidations

    assert (w.sold_size, w.last) == (Decimal(0), None)


@pytest.mark.parametrize(
    ("available", "oi_age_s", "known"),
    [(True, 0, True), (True, 31, False), (False, 0, False)],
)
def test_liquidations_are_known_only_with_the_feed_up(available, oi_age_s, known):
    b = book(liquidations_available=available)
    b.apply_liquidation(liq(T))
    b.apply_open_interest(Decimal(1), T)

    w = b.snapshot(T + oi_age_s * SECOND_NS).liquidations

    assert w.known is known
    assert w.sold_size == (Decimal("0.5") if known else Decimal(0))


def test_liquidations_without_open_interest_are_unknown():
    b = book()
    b.apply_liquidation(liq(T))

    assert b.snapshot(T).liquidations.known is False


def test_a_snapshot_never_depends_on_earlier_snapshots():
    def build():
        b = book()
        b.apply_open_interest(Decimal(1), T)
        for i in range(5):
            b.apply_liquidation(liq(T + i * 20 * SECOND_NS, size="1"))
        return b

    probed = build()
    for i in range(5):
        probed.snapshot(T + (80 + i) * SECOND_NS)
    assert probed.snapshot(T + 80 * SECOND_NS) == build().snapshot(T + 80 * SECOND_NS)


def test_rows_are_pruned_on_apply():
    b = book()
    b.apply_liquidation(liq(T))
    b.apply_mark(Decimal(1), T + WINDOW)

    assert len(b._liquidations) == 0


def test_an_older_liquidation_is_ignored():
    b = book()
    b.apply_open_interest(Decimal(1), T)
    b.apply_liquidation(liq(T, kind=LiquidationKind.TERMINATION))
    b.apply_liquidation(liq(T - 1))

    assert b.snapshot(T).liquidations.sold_size == Decimal(0)


def test_the_window_must_be_positive():
    with pytest.raises(ValueError, match="liquidation window"):
        ContextBook(AGES, 0, liquidations_available=True)


def test_log_fields_hold_strings_ages_in_seconds_and_nones():
    from trading_core.marketdata.context import context_log_fields

    b = book()
    b.apply_funding(Decimal("0.000014034466666667"), T + 900 * SECOND_NS, T)

    fields = context_log_fields(b.snapshot(T + 1_500_000_000), T + 1_500_000_000)

    assert fields["funding_rate"] == "0.000014034466666667"
    assert fields["funding_rate_age_s"] == "1.500"
    assert fields["funding_rate_stale"] is False
    assert (fields["mark"], fields["mark_age_s"], fields["mark_stale"]) == (None, None, None)
    assert fields["next_funding_ns"] == T + 900 * SECOND_NS
    assert (fields["liquidations_known"], fields["liquidations_sold_size"]) == (False, "0")

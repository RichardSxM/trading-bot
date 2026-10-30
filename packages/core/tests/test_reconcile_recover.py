"""covers: spec 0023 AC-11 (``recover`` matches Kraken's fills to local only orders by venue order id, else client
order id, skips fills the cache holds, sorts by venue time; ``window_complete`` refuses a page that may have left
fills out), AC-8 (the untrusted set's key), and AC-9 (``uses_stop`` turns off the stop check) for the pure core."""

from decimal import Decimal

from test_reconcile_compare import BTC, ETH, OURS, _exchange, _held, _local, _order, _position

from trading_core.reconcile.compare import compare
from trading_core.reconcile.recover import KRAKEN_FILLS_PAGE, cache_untrusted_key, recover, window_complete
from trading_core.reconcile.views import FillView, LocalView
from trading_core.recording.enums import MismatchKind, OrderSide


def _fill(trade="T-1", venue="V-1", client=None, qty="0.001", ts=10, instrument=BTC):
    return FillView(
        trade_id=trade,
        instrument_id=instrument,
        venue_order_id=venue,
        client_order_id=client,
        side=OrderSide.BUY,
        quantity=Decimal(qty),
        price=Decimal("60000"),
        ts_event=ts,
    )


def test_a_fill_matches_by_venue_order_id():
    (recovery,) = recover([_order()], [_fill(), _fill("T-2", "V-other")], since_ns=0)

    assert [f.trade_id for f in recovery.fills] == ["T-1"]
    assert (recovery.filled_qty, recovery.complete) == (Decimal("0.001"), True)


def test_an_order_without_a_venue_id_matches_by_client_order_id():
    order = _order("O-1", None)

    (recovery,) = recover([order], [_fill(venue="V-9", client="O-1"), _fill("T-2", "V-8", "O-2")], since_ns=0)

    assert [f.trade_id for f in recovery.fills] == ["T-1"]


def test_an_order_with_a_venue_id_ignores_a_client_id_match_on_another_venue_order():
    """The venue id is the stronger key: a reused client id on another order is not ours."""
    assert recover([_order()], [_fill(venue="V-2", client="O-1")], since_ns=0) == ()


def test_fills_on_another_instrument_do_not_match():
    assert recover([_order()], [_fill(instrument=ETH)], since_ns=0) == ()


def test_fills_the_cache_already_holds_are_skipped():
    order = _order(trade_ids=frozenset({"T-1"}), filled_qty=Decimal("0.0004"))

    (recovery,) = recover([order], [_fill(qty="0.0004"), _fill("T-2", qty="0.0006", ts=20)], since_ns=0)

    assert [f.trade_id for f in recovery.fills] == ["T-2"]
    assert (recovery.filled_qty, recovery.complete) == (Decimal("0.0010"), True)


def test_an_order_whose_fills_are_all_held_needs_no_recovery():
    assert recover([_order(trade_ids=frozenset({"T-1"}))], [_fill()], since_ns=0) == ()


def test_fills_come_oldest_first_once_each():
    fills = [_fill("T-2", qty="0.0005", ts=30), _fill("T-1", qty="0.0003", ts=20), _fill("T-1", qty="0.0003", ts=20)]

    (recovery,) = recover([_order()], fills, since_ns=0)

    assert [f.trade_id for f in recovery.fills] == ["T-1", "T-2"]
    assert (recovery.filled_qty, recovery.complete) == (Decimal("0.0008"), False)


def test_fills_before_the_window_are_ignored():
    assert recover([_order()], [_fill(ts=5)], since_ns=6) == ()


def test_a_short_page_is_complete():
    assert window_complete([_fill(ts=100)] * (KRAKEN_FILLS_PAGE - 1), since_ns=50)


def test_a_full_page_reaching_past_the_window_start_is_complete():
    assert window_complete([_fill(ts=100)] * (KRAKEN_FILLS_PAGE - 1) + [_fill(ts=40)], since_ns=50)


def test_a_full_page_inside_the_window_may_have_left_fills_out():
    assert not window_complete([_fill(ts=100)] * KRAKEN_FILLS_PAGE, since_ns=50)


def test_the_untrusted_set_key():
    assert cache_untrusted_key("live") == "live:reconcile:cache_untrusted"


def test_a_stopless_strategy_s_position_needs_no_stop():
    """AC-9: ``BuyAndHold`` and ``RandomTaker`` never place one."""
    plain = _local(positions=[_held()])
    stopless = LocalView(**{**plain.__dict__, "stopless_strategies": frozenset({OURS})})

    assert [f.kind for f in compare(_exchange(positions=[_position()]), plain).findings] == [MismatchKind.MISSING_STOP]
    assert compare(_exchange(positions=[_position()]), stopless).clean

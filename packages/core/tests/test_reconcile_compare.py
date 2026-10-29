"""covers: spec 0023 AC-7, AC-8, AC-10, AC-11 (the order and position matching of the pure ``compare``)."""

from decimal import Decimal

from trading_core.reconcile.compare import compare, rounded
from trading_core.reconcile.views import ExchangeView, LocalView, OrderView, PositionView, exchange_snapshot_data
from trading_core.recording.enums import MismatchKind, OrderSide, PositionSide

BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"


def _order(client="O-1", venue="V-1", instrument=BTC, side=OrderSide.BUY, qty="0.001", **kw):
    return OrderView(
        instrument_id=instrument,
        client_order_id=client,
        venue_order_id=venue,
        side=side,
        order_type=kw.pop("order_type", "limit"),
        quantity=Decimal(qty),
        **kw,
    )


def _position(instrument=BTC, side=PositionSide.LONG, qty="0.001"):
    return PositionView(instrument_id=instrument, side=side, quantity=Decimal(qty))


def _exchange(orders=(), positions=()):
    return ExchangeView(orders=tuple(orders), positions=tuple(positions), fetched_ns=1)


def _local(orders=(), positions=(), increments=None):
    return LocalView(
        orders=tuple(orders), positions=tuple(positions), size_increments=increments or {BTC: Decimal("0.0001")}
    )


def test_two_empty_views_are_clean():
    assert compare(_exchange(), _local()).clean


def test_the_same_orders_and_position_are_clean():
    orders = [_order(), _order("O-2", "V-2", side=OrderSide.SELL, order_type="stop_market", reduce_only=True)]

    result = compare(_exchange(orders, [_position()]), _local(orders, [_position()]))

    assert result.clean
    assert result.findings == ()


def test_orders_match_on_the_venue_id_even_when_the_client_id_differs():
    """Kraken may report its own client id; the venue id decides."""
    assert compare(_exchange([_order(client="kraken-cl")]), _local([_order()])).clean


def test_orders_fall_back_to_the_client_id_when_one_side_has_no_venue_id():
    assert compare(_exchange([_order()]), _local([_order(venue=None)])).clean


def test_a_different_venue_id_is_a_different_order_whatever_the_client_id():
    result = compare(_exchange([_order(venue="V-9")]), _local([_order()]))

    assert [f.kind for f in result.findings] == [MismatchKind.UNKNOWN_ORDER, MismatchKind.LOCAL_ONLY_ORDER]


def test_prices_and_sizes_of_orders_are_not_compared():
    exchange = _order(qty="0.002", trigger_price=Decimal("80000"))

    assert compare(_exchange([exchange]), _local([_order()])).clean


def test_an_exchange_order_the_trader_does_not_hold_is_unknown_order():
    """AC-10."""
    foreign = _order("drill-foreign-1", "V-7", side=OrderSide.SELL, qty="0.0001")

    result = compare(_exchange([foreign]), _local())

    (finding,) = result.findings
    assert finding.kind is MismatchKind.UNKNOWN_ORDER
    assert (finding.client_order_id, finding.venue_order_id) == ("drill-foreign-1", "V-7")
    assert (finding.exchange_side, finding.exchange_qty) == ("sell", Decimal("0.0001"))
    assert (finding.local_side, finding.local_qty) == (None, None)
    assert not result.clean


def test_a_local_order_the_exchange_does_not_have_is_local_only_order():
    """AC-11."""
    (finding,) = compare(_exchange(), _local([_order()])).findings

    assert finding.kind is MismatchKind.LOCAL_ONLY_ORDER
    assert (finding.local_side, finding.local_qty) == ("buy", Decimal("0.001"))
    assert finding.exchange_side is None


def test_an_exchange_position_with_no_local_one_is_unknown_position():
    """AC-7."""
    (finding,) = compare(_exchange(positions=[_position(qty="0.0001")]), _local()).findings

    assert finding.kind is MismatchKind.UNKNOWN_POSITION
    assert (finding.exchange_side, finding.exchange_qty) == ("long", Decimal("0.0001"))
    assert finding.local_side is None


def test_a_local_position_the_exchange_does_not_have_is_position_mismatch():
    """AC-8."""
    (finding,) = compare(_exchange(), _local(positions=[_position()])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert (finding.local_side, finding.local_qty) == ("long", Decimal("0.001"))
    assert finding.exchange_side is None


def test_a_different_side_is_position_mismatch():
    (finding,) = compare(
        _exchange(positions=[_position(side=PositionSide.SHORT)]), _local(positions=[_position()])
    ).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert (finding.exchange_side, finding.local_side) == ("short", "long")


def test_a_different_size_is_position_mismatch():
    """A partial fill the trader did not see."""
    (finding,) = compare(_exchange(positions=[_position(qty="0.0015")]), _local(positions=[_position()])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert (finding.exchange_qty, finding.local_qty) == (Decimal("0.0015"), Decimal("0.001"))


def test_sizes_are_equal_after_rounding_both_to_the_size_increment():
    exchange = _exchange(positions=[_position(qty="0.00100000001")])

    assert compare(exchange, _local(positions=[_position(qty="0.001")])).clean


def test_without_a_size_increment_sizes_are_compared_exactly():
    exchange = _exchange(positions=[_position(qty="0.00100000001")])

    assert not compare(exchange, _local(positions=[_position()], increments={ETH: Decimal(1)})).clean


def test_findings_come_in_a_fixed_order_orders_first_then_positions_by_instrument():
    exchange = _exchange([_order("O-b", "V-b"), _order("O-a", "V-a")], [_position(ETH), _position(BTC)])

    result = compare(exchange, _local([_order("O-z", "V-z")]))

    assert [(f.kind, f.client_order_id or f.instrument_id) for f in result.findings] == [
        (MismatchKind.UNKNOWN_ORDER, "O-a"),
        (MismatchKind.UNKNOWN_ORDER, "O-b"),
        (MismatchKind.LOCAL_ONLY_ORDER, "O-z"),
        (MismatchKind.UNKNOWN_POSITION, ETH),  # PF_ETHUSD sorts before PF_XBTUSD
        (MismatchKind.UNKNOWN_POSITION, BTC),
    ]


def test_rounded_snaps_to_the_nearest_multiple():
    assert rounded(Decimal("0.00015"), Decimal("0.0001")) == Decimal("0.0002")
    assert rounded(Decimal("0.00025"), Decimal("0.0001")) == Decimal("0.0002")  # half even
    assert rounded(Decimal("7"), Decimal("5")) == Decimal("5")
    assert rounded(Decimal("0.123"), None) == Decimal("0.123")


def test_the_exchange_snapshot_lists_what_the_exchange_reported():
    # covers: spec 0023 data model, ``{env}:snapshot:exchange``.
    order = OrderView(
        instrument_id="PF_XBTUSD.KRAKEN",
        client_order_id=None,
        venue_order_id="V-1",
        side=OrderSide.SELL,
        order_type="stop_market",
        quantity=Decimal("0.0010"),
        trigger_price=Decimal("60000.0"),
        reduce_only=True,
    )
    position = PositionView(instrument_id="PF_XBTUSD.KRAKEN", side=PositionSide.LONG, quantity=Decimal("0.0010"))

    data = exchange_snapshot_data(ExchangeView(orders=(order,), positions=(position,), fetched_ns=5), 9)

    assert data == {
        "status": "ok",
        "fetched_ns": 5,
        "positions": [{"instrument_id": "PF_XBTUSD.KRAKEN", "side": "long", "quantity": "0.0010"}],
        "orders": [
            {
                "client_order_id": None,
                "venue_order_id": "V-1",
                "instrument_id": "PF_XBTUSD.KRAKEN",
                "side": "sell",
                "order_type": "stop_market",
                "quantity": "0.0010",
                "trigger_price": "60000.0",
                "reduce_only": True,
            }
        ],
    }


def test_a_failed_fetch_snapshot_is_unknown_with_no_orders_or_positions():
    assert exchange_snapshot_data(None, 9) == {"status": "unknown", "fetched_ns": 9}

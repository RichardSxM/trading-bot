"""covers: spec 0023 AC-6 (a known position is adopted), AC-7, AC-8, AC-9 (the stop check), AC-10, AC-11, AC-12
(the order, position, stop, and duplicate findings of the pure ``compare``)."""

from decimal import Decimal

from trading_core.reconcile.compare import compare, known_positions, rounded
from trading_core.reconcile.views import (
    ExchangeView,
    FillOrder,
    HeldPosition,
    LocalView,
    OrderView,
    PositionView,
    exchange_snapshot_data,
)
from trading_core.recording.enums import MismatchKind, OrderSide, PositionSide

BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
OURS = "Scalp-001"
TRIGGER = Decimal("60000")


def _order(client="O-1", venue="V-1", instrument=BTC, side=OrderSide.BUY, qty="0.001", **kw):
    return OrderView(
        instrument_id=instrument,
        client_order_id=client,
        venue_order_id=venue,
        side=side,
        order_type=kw.pop("order_type", "limit"),
        quantity=Decimal(qty),
        strategy_id=kw.pop("strategy_id", OURS),
        **kw,
    )


def _stop(client="S-1", venue="VS-1", qty="0.001", trigger=TRIGGER, side=OrderSide.SELL, instrument=BTC):
    return _order(
        client, venue, instrument, side, qty, order_type="stop_market", reduce_only=True, trigger_price=trigger
    )


def _position(instrument=BTC, side=PositionSide.LONG, qty="0.001"):
    return PositionView(instrument_id=instrument, side=side, quantity=Decimal(qty))


def _held(instrument=BTC, side=PositionSide.LONG, qty="0.001", strategy=OURS, fills=None, position_id=None):
    fills = fills if fills is not None else (FillOrder(client_order_id="E-1", strategy_id=strategy, reduce_only=False),)
    return HeldPosition(
        instrument_id=instrument,
        side=side,
        quantity=Decimal(qty),
        position_id=position_id or f"{instrument}-{strategy}",
        strategy_id=strategy,
        opening_order_id=fills[0].client_order_id if fills else None,
        ts_opened=1,
        fill_orders=tuple(fills),
    )


def _exchange(orders=(), positions=()):
    return ExchangeView(orders=tuple(orders), positions=tuple(positions), fetched_ns=1)


def _local(orders=(), positions=(), increments=None, triggers=None):
    return LocalView(
        orders=tuple(orders),
        positions=tuple(positions),
        size_increments=increments or {BTC: Decimal("0.0001")},
        owned_strategies=frozenset({OURS}),
        stop_triggers={BTC: TRIGGER, ETH: TRIGGER} if triggers is None else triggers,
    )


def _kinds(result):
    return [f.kind for f in result.findings]


def test_two_empty_views_are_clean():
    assert compare(_exchange(), _local()).clean


def test_the_same_orders_and_a_known_position_with_its_stop_are_clean():
    orders = [_order(), _stop()]

    result = compare(_exchange(orders, [_position()]), _local(orders, [_held()]))

    assert result.clean
    assert result.findings == ()


def test_orders_match_on_the_venue_id_even_when_the_client_id_differs():
    """Kraken may report its own client id; the venue id decides."""
    assert compare(_exchange([_order(client="kraken-cl")]), _local([_order()])).clean


def test_orders_fall_back_to_the_client_id_when_one_side_has_no_venue_id():
    assert compare(_exchange([_order()]), _local([_order(venue=None)])).clean


def test_a_different_venue_id_is_a_different_order_whatever_the_client_id():
    result = compare(_exchange([_order(venue="V-9")]), _local([_order()]))

    assert _kinds(result) == [MismatchKind.UNKNOWN_ORDER, MismatchKind.LOCAL_ONLY_ORDER]


def test_prices_and_sizes_of_orders_are_not_compared():
    exchange = _order(qty="0.002", trigger_price=Decimal("80000"))

    assert compare(_exchange([exchange]), _local([_order()])).clean


def test_an_exchange_order_the_trader_does_not_hold_is_unknown_order():
    """AC-10."""
    foreign = _order("drill-foreign-1", "V-7", side=OrderSide.SELL, qty="0.0001", strategy_id=None)

    result = compare(_exchange([foreign]), _local())

    (finding,) = result.findings
    assert finding.kind is MismatchKind.UNKNOWN_ORDER
    assert (finding.client_order_id, finding.venue_order_id) == ("drill-foreign-1", "V-7")
    assert (finding.exchange_side, finding.exchange_qty) == ("sell", Decimal("0.0001"))
    assert (finding.local_side, finding.local_qty) == (None, None)
    assert finding.orders == (foreign,)
    assert not result.clean


def test_a_local_order_the_exchange_does_not_have_is_local_only_order():
    """AC-11."""
    (finding,) = compare(_exchange(), _local([_order()])).findings

    assert finding.kind is MismatchKind.LOCAL_ONLY_ORDER
    assert (finding.local_side, finding.local_qty) == ("buy", Decimal("0.001"))
    assert finding.exchange_side is None


def test_an_exchange_position_with_no_held_one_is_unknown_position():
    """AC-7."""
    (finding,) = compare(_exchange(positions=[_position(qty="0.0001")]), _local()).findings

    assert finding.kind is MismatchKind.UNKNOWN_POSITION
    assert (finding.exchange_side, finding.exchange_qty) == ("long", Decimal("0.0001"))
    assert finding.local_side is None
    assert finding.exchange_position == _position(qty="0.0001")


def test_a_held_position_the_exchange_does_not_have_is_position_mismatch():
    """AC-8."""
    (finding,) = compare(_exchange(), _local(positions=[_held()])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert (finding.local_side, finding.local_qty) == ("long", Decimal("0.001"))
    assert finding.exchange_side is None
    assert finding.reason == "not_on_exchange"


def test_a_different_side_is_position_mismatch():
    (finding,) = compare(
        _exchange(positions=[_position(side=PositionSide.SHORT)]), _local(positions=[_held()])
    ).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert (finding.exchange_side, finding.local_side) == ("short", "long")


def test_a_size_our_fills_do_not_explain_is_position_mismatch():
    """A partial fill the trader did not see."""
    (finding,) = compare(_exchange(positions=[_position(qty="0.0015")]), _local(positions=[_held()])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert (finding.exchange_qty, finding.local_qty) == (Decimal("0.0015"), Decimal("0.001"))


def test_a_partial_fill_our_own_fills_explain_is_known_and_adopted():
    """AC-6: an entry partly filled before a crash, the rest never filled."""
    held = _held(qty="0.0004")
    stop = _stop(qty="0.0004")

    result = compare(_exchange([stop], [_position(qty="0.0004")]), _local([stop], [held]))

    assert result.clean
    assert known_positions(_exchange([stop], [_position(qty="0.0004")]), _local([stop], [held])) == (held,)


def test_a_position_filled_by_a_generated_order_is_not_ours():
    """AC-6: Nautilus's reconciliation fills (a lost Redis cache) are never ours."""
    fills = (FillOrder(client_order_id="R-1", strategy_id=OURS, reduce_only=False, generated=True),)

    (finding,) = compare(_exchange([_stop()], [_position()]), _local([_stop()], [_held(fills=fills)])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert finding.reason == "fills_not_ours"


def test_a_position_of_a_strategy_that_is_not_loaded_is_not_ours():
    """AC-6, AC-16: ``EXTERNAL`` or an unloaded strategy."""
    (finding,) = compare(_exchange(positions=[_position()]), _local(positions=[_held(strategy="EXTERNAL")])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert finding.reason == "fills_not_ours"
    assert known_positions(_exchange(positions=[_position()]), _local(positions=[_held(strategy="EXTERNAL")])) == ()


def test_a_position_with_no_fills_on_record_is_not_ours():
    (finding,) = compare(_exchange(positions=[_position()]), _local(positions=[_held(fills=())])).findings

    assert finding.reason == "fills_not_ours"


def test_a_position_split_between_owners_is_position_mismatch_with_the_net_size():
    """Nautilus adds an ``EXTERNAL`` position next to ours for size our fills do not explain."""
    ours = _held(qty="0.0005")
    external = _held(qty="0.0005", strategy="EXTERNAL")

    (finding,) = compare(_exchange(positions=[_position()]), _local(positions=[ours, external])).findings

    assert finding.kind is MismatchKind.POSITION_MISMATCH
    assert finding.reason == "split_ownership"
    assert (finding.local_side, finding.local_qty) == ("long", Decimal("0.0010"))
    assert finding.position is None


def test_sizes_are_equal_after_rounding_both_to_the_size_increment():
    exchange = _exchange([_stop()], positions=[_position(qty="0.00100000001")])

    assert compare(exchange, _local([_stop()], positions=[_held(qty="0.001")])).clean


def test_without_a_size_increment_sizes_are_compared_exactly():
    exchange = _exchange(positions=[_position(qty="0.00100000001")])

    assert not compare(exchange, _local(positions=[_held()], increments={ETH: Decimal(1)})).clean


def test_a_known_position_without_a_stop_is_missing_stop():
    """AC-9."""
    (finding,) = compare(_exchange(positions=[_position()]), _local(positions=[_held()])).findings

    assert finding.kind is MismatchKind.MISSING_STOP
    assert finding.orders == ()
    assert finding.position == _held()
    assert (finding.local_side, finding.local_qty) == ("long", Decimal("0.001"))


def test_a_stop_of_the_wrong_size_is_stop_size_mismatch():
    """AC-9."""
    half = _stop(qty="0.0005")

    (finding,) = compare(_exchange([half], [_position()]), _local([half], [_held()])).findings

    assert finding.kind is MismatchKind.STOP_SIZE_MISMATCH
    assert finding.orders == (half,)
    assert finding.exchange_qty == Decimal("0.0005")


def test_two_stops_are_stop_size_mismatch_even_when_one_is_right():
    """AC-9: a crash between placing the new stop and cancelling the old one."""
    stops = [_stop(), _stop("S-2", "VS-2", qty="0.0005")]

    (finding,) = compare(_exchange(stops, [_position()]), _local(stops, [_held()])).findings

    assert finding.kind is MismatchKind.STOP_SIZE_MISMATCH
    assert [s.client_order_id for s in finding.orders] == ["S-1", "S-2"]


def test_a_stop_at_another_price_than_the_recorded_one_is_stop_size_mismatch():
    stop = _stop(trigger=Decimal("59000"))

    (finding,) = compare(_exchange([stop], [_position()]), _local([stop], [_held()])).findings

    assert finding.kind is MismatchKind.STOP_SIZE_MISMATCH


def test_a_stop_with_no_recorded_trigger_price_is_a_stop_finding():
    (finding,) = compare(_exchange([_stop()], [_position()]), _local([_stop()], [_held()], triggers={})).findings

    assert finding.kind is MismatchKind.STOP_SIZE_MISMATCH


def test_a_stop_on_the_wrong_side_is_stop_size_mismatch():
    stop = _stop(side=OrderSide.BUY)

    (finding,) = compare(_exchange([stop], [_position()]), _local([stop], [_held()])).findings

    assert finding.kind is MismatchKind.STOP_SIZE_MISMATCH


def test_a_short_position_needs_a_buy_stop():
    stop = _stop(side=OrderSide.BUY, trigger=Decimal("70000"))
    local = _local([stop], [_held(side=PositionSide.SHORT)], triggers={BTC: Decimal("70000")})

    assert compare(_exchange([stop], [_position(side=PositionSide.SHORT)]), local).clean


def test_a_foreign_stop_is_only_an_unknown_order_and_never_counts_as_the_positions_stop():
    """The foreign stop is cancelled (AC-10), so the position still needs its own."""
    foreign = _stop("drill-foreign-1", "V-7")

    result = compare(_exchange([foreign], [_position()]), _local(positions=[_held()]))

    assert _kinds(result) == [MismatchKind.UNKNOWN_ORDER, MismatchKind.MISSING_STOP]


def test_two_open_entry_orders_of_one_strategy_are_duplicate_entry():
    """AC-12."""
    entries = [_order(), _order("O-2", "V-2")]

    (finding,) = compare(_exchange(entries), _local(entries)).findings

    assert finding.kind is MismatchKind.DUPLICATE_ENTRY
    assert finding.reason == "open_entries"
    assert [o.client_order_id for o in finding.orders] == ["O-1", "O-2"]


def test_entries_of_two_strategies_or_an_entry_and_a_stop_are_not_duplicates():
    orders = [_order(), _order("O-2", "V-2", strategy_id="Other-001"), _stop()]

    assert MismatchKind.DUPLICATE_ENTRY not in _kinds(compare(_exchange(orders), _local(orders)))


def test_a_position_two_entry_orders_filled_is_duplicate_entry_and_skips_the_stop_check():
    """AC-12: a lost acknowledgement, then the same entry sent again."""
    fills = (
        FillOrder(client_order_id="E-1", strategy_id=OURS, reduce_only=False),
        FillOrder(client_order_id="E-2", strategy_id=OURS, reduce_only=False),
    )

    result = compare(_exchange(positions=[_position()]), _local(positions=[_held(fills=fills)]))

    (finding,) = result.findings
    assert finding.kind is MismatchKind.DUPLICATE_ENTRY
    assert finding.reason == "filled_entries"


def test_an_entry_and_its_partial_exit_are_not_duplicates():
    fills = (
        FillOrder(client_order_id="E-1", strategy_id=OURS, reduce_only=False),
        FillOrder(client_order_id="X-1", strategy_id=OURS, reduce_only=True),
    )

    assert compare(_exchange([_stop()], [_position()]), _local([_stop()], [_held(fills=fills)])).clean


def test_findings_come_in_a_fixed_order():
    exchange = _exchange([_order("O-b", "V-b"), _order("O-a", "V-a")], [_position(ETH), _position(BTC)])

    result = compare(exchange, _local([_order("O-z", "V-z")]))

    assert [(f.kind, f.client_order_id or f.instrument_id) for f in result.findings] == [
        (MismatchKind.UNKNOWN_ORDER, "O-a"),
        (MismatchKind.UNKNOWN_ORDER, "O-b"),
        (MismatchKind.LOCAL_ONLY_ORDER, "O-z"),
        (MismatchKind.UNKNOWN_POSITION, ETH),  # PF_ETHUSD sorts before PF_XBTUSD
        (MismatchKind.UNKNOWN_POSITION, BTC),
    ]


def test_a_findings_key_names_its_kind_instrument_and_order():
    (finding,) = compare(_exchange([_order("O-a", "V-a")]), _local()).findings

    assert finding.key == ("unknown_order", BTC, "V-a", "O-a")


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

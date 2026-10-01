"""covers: spec 0002 enum CHECKs list every member of the pinned Nautilus 1.231.0 enums.

Runs only where NautilusTrader is installed (the trader image); skipped elsewhere.
"""

import inspect
import re

import pytest

from trading_core.recording import enums

nautilus_enums = pytest.importorskip("nautilus_trader.model.enums")
order_events = pytest.importorskip("nautilus_trader.model.events.order")


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _names(enum_name: str) -> set[str]:
    return {member.name.lower() for member in getattr(nautilus_enums, enum_name)}


@pytest.mark.parametrize(
    ("ours", "theirs", "left_out"),
    [
        (enums.OrderSide, "OrderSide", {"no_order_side"}),
        (enums.OrderType, "OrderType", set()),
        (enums.TimeInForce, "TimeInForce", set()),
        (enums.PositionSide, "PositionSide", {"no_position_side"}),
    ],
)
def test_nautilus_enums_are_mirrored_in_snake_case(ours, theirs, left_out):
    assert {member.value for member in ours} == _names(theirs) - left_out


def test_order_status_is_nautilus_plus_unknown():
    assert {member.value for member in enums.OrderStatus} == _names("OrderStatus") | {"unknown"}


def test_liquidity_maps_no_liquidity_side_to_no_liquidity():
    assert _names("LiquiditySide") == {"maker", "taker", "no_liquidity_side"}
    assert {member.value for member in enums.Liquidity} == {"maker", "taker", "no_liquidity"}


def test_order_event_kinds_are_every_nautilus_order_event_class():
    classes = {
        _snake(name)
        for name, cls in vars(order_events).items()
        if inspect.isclass(cls) and name.startswith("Order") and name != "OrderEvent"
    }

    assert {member.value for member in enums.OrderEventKind} == classes

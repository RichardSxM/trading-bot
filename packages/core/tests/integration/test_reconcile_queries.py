"""covers: spec 0023 AC-9 (the Postgres fallback for a stop's trigger price: the newest recorded reduce only stop
market order whose parent is the position's opening order)."""

from decimal import Decimal
from uuid import uuid7

from sqlalchemy.orm import Session

from trading_core.recording import repository
from trading_core.recording.enums import Env, OrderOrigin, OrderSide, OrderType
from trading_core.recording.queries import recorded_stop_trigger

from .conftest import World


def _stop(world: World, parent: str, trigger: str, **overrides: object):
    values: dict[str, object] = {
        "origin": OrderOrigin.RECONCILIATION,
        "side": OrderSide.SELL,
        "order_type": OrderType.STOP_MARKET,
        "price": None,
        "trigger_price": Decimal(trigger),
        "reduce_only": True,
        "parent_client_order_id": parent,
    }
    return world.order(**(values | overrides))


def test_the_newest_stop_of_the_opening_order_gives_the_price(session: Session, world: World):
    parent = f"E-{uuid7().hex[-12:]}"
    repository.upsert_order(session, _stop(world, parent, "60000"))
    session.flush()
    repository.upsert_order(session, _stop(world, parent, "60500"))
    session.flush()

    assert recorded_stop_trigger(session, Env.DEMO, parent) == Decimal("60500")


def test_other_orders_and_other_parents_never_give_a_price(session: Session, world: World):
    parent = f"E-{uuid7().hex[-12:]}"
    repository.upsert_order(session, _stop(world, parent, "61000", reduce_only=False))
    repository.upsert_order(session, _stop(world, parent, "62000", order_type=OrderType.STOP_LIMIT, price=Decimal(1)))
    repository.upsert_order(session, _stop(world, "E-someone-else", "63000"))
    session.flush()

    assert recorded_stop_trigger(session, Env.DEMO, parent) is None
    assert recorded_stop_trigger(session, Env.LIVE, "E-someone-else") is None

"""covers: spec 0023 Interface surface, ``python -m trader.drill`` (drills D4, D6, D7, D12): it refuses unless
``TRADING_ENV=live`` and ``LIVE_MODE=drill``; ``foreign-order`` rests a post only buy of the drill size far below
the mark and ``foreign-position`` buys the drill size at market, both under a ``drill-foreign-<ns>`` id the trader
never saw; ``cancel-stop`` cancels the resting reduce only stops; ``show`` prints Kraken's view."""

import asyncio
from decimal import Decimal

import pytest

from trader import drill
from trader.drill import DrillRefused, InstrumentSpec, run
from trading_core.node.kraken import BTC_PERP
from trading_core.reconcile.views import OrderView, PositionView
from trading_core.recording.enums import OrderSide, PositionSide
from trading_core.settings import ReconcileSettings

NS = 1_790_000_000_000_000_000
STOP = OrderView(
    instrument_id=BTC_PERP,
    client_order_id="O-1-stop",
    venue_order_id="v-stop",
    side=OrderSide.SELL,
    order_type="stop_market",
    quantity=Decimal("0.0001"),
    trigger_price=Decimal(75_000),
    reduce_only=True,
)
ENTRY = OrderView(
    instrument_id=BTC_PERP,
    client_order_id="O-1-rest",
    venue_order_id="v-rest",
    side=OrderSide.BUY,
    order_type="limit",
    quantity=Decimal("0.0001"),
)
HELD = PositionView(instrument_id=BTC_PERP, side=PositionSide.LONG, quantity=Decimal("0.0001"))


class FakeKraken:
    def __init__(self, orders=(), positions=(), minimum=None):
        self._orders, self._positions = list(orders), list(positions)
        self._spec = InstrumentSpec(Decimal("0.0001"), minimum, Decimal(1))
        self.bought, self.cancelled = [], []

    async def spec(self, instrument_id):
        return self._spec

    async def mark(self, instrument_id):
        return Decimal("76971.43")

    async def orders(self):
        return self._orders

    async def positions(self):
        return self._positions

    async def buy(self, instrument_id, client_order_id, quantity, price):
        self.bought.append((instrument_id, client_order_id, quantity, price))

    async def cancel(self, order):
        self.cancelled.append(order.venue_order_id)


def _settings(env="live", live_mode="drill", **extra):
    return ReconcileSettings(trading_env=env, live_mode=live_mode, **extra)


def _run(action, kraken, settings=None):
    return asyncio.run(run(action, kraken, settings or _settings(), now_ns=lambda: NS))


@pytest.mark.parametrize(("env", "mode"), [("live", "off"), ("demo", "drill"), ("demo", "off")])
@pytest.mark.parametrize("action", drill.ACTIONS)
def test_it_refuses_outside_live_drill_mode(env, mode, action):
    kraken = FakeKraken()

    with pytest.raises(DrillRefused, match="LIVE_MODE=drill"):
        _run(action, kraken, _settings(env, mode))
    assert (kraken.bought, kraken.cancelled) == ([], [])


def test_the_key_files_are_read_only_in_drill_mode():
    assert drill.drill_secrets(_settings()) == ("kraken_api_key", "kraken_api_secret")
    assert drill.drill_secrets(_settings("live", "off")) == ()


def test_a_foreign_order_rests_far_below_the_mark_under_an_id_the_trader_never_saw():
    kraken = FakeKraken()

    result = _run("foreign-order", kraken)

    # 76 971.43 x 0.8 = 61 577.14, rounded down to the 1 USD tick.
    assert kraken.bought == [(BTC_PERP, f"drill-foreign-{NS}", Decimal("0.0001"), Decimal(61577))]
    assert result == {
        "sent": "limit_buy",
        "client_order_id": f"drill-foreign-{NS}",
        "quantity": "0.0001",
        "price": "61577",
    }


def test_the_foreign_order_distance_comes_from_the_setting():
    kraken = FakeKraken()

    _run("foreign-order", kraken, _settings(drill_rest_distance_pct=Decimal(50)))

    assert kraken.bought[0][3] == Decimal(38485)


def test_a_foreign_position_buys_the_drill_size_at_market():
    kraken = FakeKraken(minimum=Decimal("0.0002"))

    result = _run("foreign-position", kraken)

    assert kraken.bought == [(BTC_PERP, f"drill-foreign-{NS}", Decimal("0.0002"), None)]
    assert result["sent"] == "market_buy"


def test_a_foreign_position_is_refused_while_kraken_holds_one():
    kraken = FakeKraken(positions=[HELD])

    with pytest.raises(DrillRefused, match="already holds"):
        _run("foreign-position", kraken)
    assert kraken.bought == []


def test_cancel_stop_cancels_only_the_reduce_only_stops():
    kraken = FakeKraken(orders=[STOP, ENTRY])

    result = _run("cancel-stop", kraken)

    assert kraken.cancelled == ["v-stop"]
    assert [o["client_order_id"] for o in result["cancelled"]] == ["O-1-stop"]


def test_cancel_stop_with_no_stop_is_refused():
    with pytest.raises(DrillRefused, match="no reduce only stop"):
        _run("cancel-stop", FakeKraken(orders=[ENTRY]))


def test_show_prints_kraken_s_orders_and_positions():
    result = _run("show", FakeKraken(orders=[STOP], positions=[HELD]))

    assert result == {
        "orders": [
            {
                "client_order_id": "O-1-stop",
                "venue_order_id": "v-stop",
                "instrument_id": BTC_PERP,
                "side": "sell",
                "order_type": "stop_market",
                "quantity": "0.0001",
                "trigger_price": "75000",
                "reduce_only": True,
            }
        ],
        "positions": [{"instrument_id": BTC_PERP, "side": "long", "quantity": "0.0001"}],
    }


@pytest.mark.parametrize("argv", [[], ["bogus"], ["show", "extra"]])
def test_a_wrong_command_exits_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        drill.main(argv)

    assert exc.value.code == 2
    assert "usage: python -m trader.drill" in capsys.readouterr().err


def test_main_refuses_outside_drill_mode_before_any_key_file(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("LIVE_MODE", "off")
    monkeypatch.setenv("SECRETS_DIR_IN_CONTAINER", str(tmp_path))  # holds no key files

    with pytest.raises(SystemExit) as exc:
        drill.main(["show"])

    assert exc.value.code == 1
    assert "LIVE_MODE=drill" in capsys.readouterr().err

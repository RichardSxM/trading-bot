"""``python -m trader.drill <foreign-order|foreign-position|cancel-stop|extra-stop|show>``: the live drill's hand tools
(spec 0023 Interface surface, drills D4, D6, D7, D12).

It talks to Kraken directly through the Kraken Futures HTTP client, with the keys from the secret
files, behind the trader's back, so the trader meets orders and positions it never sent:

- ``foreign-order``: a post only limit buy of the drill size at the mark x (1 - ``DRILL_REST_DISTANCE_PCT``),
  client order id ``drill-foreign-<ns>`` (D4).
- ``foreign-position``: a market buy of the drill size, client order id ``drill-foreign-<ns>`` (D7).
  Refused while Kraken already holds a position on the instrument, so the drill never holds two.
- ``cancel-stop``: cancels every reduce only stop resting on the instrument (D6, D12). Exits 1 when
  there is none.
- ``extra-stop``: a second reduce only sell stop for Kraken's whole long position, ``DRILL_STOP_PCT`` below the
  lowest stop already resting, client order id ``drill-foreign-<ns>`` (D12: AC-9 counts extra stops as
  ``stop_size_mismatch``, and the drill size is too small to halve). Exits 1 without a long position and a stop.
- ``show``: Kraken's open orders and positions.

Each prints one JSON line of what Kraken returned. It refuses, exit 1, unless ``TRADING_ENV=live`` and
``LIVE_MODE=drill``. It logs no key material.
"""

import asyncio
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Final, Protocol

from nautilus_trader.core import nautilus_pyo3
from nautilus_trader.core.nautilus_pyo3 import KrakenFuturesHttpClient
from pydantic import SecretStr, ValidationError

from trading_core.boot import boot
from trading_core.node.kraken import BTC_PERP
from trading_core.node.reconcile import (
    KRAKEN_ACCOUNT_ID,
    PyO3FuturesReports,
    report_order_view,
    report_position_view,
)
from trading_core.reconcile.drill import drill_size, rest_price, stop_trigger
from trading_core.reconcile.views import OrderView, PositionView
from trading_core.recording.enums import PositionSide
from trading_core.secrets import KRAKEN_API_KEY, KRAKEN_API_SECRET
from trading_core.settings import ReconcileSettings

ACTIONS: Final = ("foreign-order", "foreign-position", "cancel-stop", "extra-stop", "show")
USAGE: Final = f"usage: python -m trader.drill <{'|'.join(ACTIONS)}>"
FOREIGN_PREFIX: Final = "drill-foreign-"
STOP_TYPES: Final = frozenset({"stop_market", "stop_limit"})


class DrillRefused(Exception):
    """The action must not run; the message says why."""


class Spec(Protocol):
    """The instrument facts the drill sizes and prices from."""

    @property
    def size_increment(self) -> Decimal: ...

    @property
    def min_quantity(self) -> Decimal | None: ...

    @property
    def price_increment(self) -> Decimal: ...


class DrillKraken(Protocol):
    """The Kraken calls the drill tools make (``PyO3DrillKraken`` in the image, a fake in tests)."""

    async def spec(self, instrument_id: str) -> Spec: ...

    async def mark(self, instrument_id: str) -> Decimal: ...

    async def orders(self) -> Sequence[OrderView]: ...

    async def positions(self) -> Sequence[PositionView]: ...

    async def buy(self, instrument_id: str, client_order_id: str, quantity: Decimal, price: Decimal | None) -> None:
        """A post only limit buy at ``price``, or a market buy when it is None."""
        ...

    async def sell_stop(self, instrument_id: str, client_order_id: str, quantity: Decimal, trigger: Decimal) -> None:
        """A reduce only stop market sell triggered at ``trigger``."""
        ...

    async def cancel(self, order: OrderView) -> None: ...


class InstrumentSpec:
    """``Spec`` from a pyo3 instrument."""

    def __init__(self, size_increment: Decimal, min_quantity: Decimal | None, price_increment: Decimal) -> None:
        self.size_increment = size_increment
        self.min_quantity = min_quantity
        self.price_increment = price_increment


class PyO3DrillKraken:
    """``DrillKraken`` through the Kraken Futures HTTP client; loads the instruments once, on first use."""

    def __init__(self, http: KrakenFuturesHttpClient) -> None:
        self._http = http
        self._reports = PyO3FuturesReports(http)
        self._account_id = nautilus_pyo3.AccountId(KRAKEN_ACCOUNT_ID)
        self._specs: dict[str, InstrumentSpec] = {}

    async def _load(self) -> None:
        if self._specs:
            return
        for instrument in await self._http.request_instruments():
            self._http.cache_instrument(instrument)
            minimum = instrument.min_quantity
            self._specs[str(instrument.id)] = InstrumentSpec(
                instrument.size_increment.as_decimal(),
                None if minimum is None else minimum.as_decimal(),
                instrument.price_increment.as_decimal(),
            )

    async def spec(self, instrument_id: str) -> Spec:
        """The instrument's size and price steps; ``DrillRefused`` when Kraken does not list it."""
        await self._load()
        found = self._specs.get(instrument_id)
        if found is None:
            raise DrillRefused(f"Kraken does not list {instrument_id}")
        return found

    async def mark(self, instrument_id: str) -> Decimal:
        """Kraken's mark price."""
        await self._load()
        mark: Decimal = await self._http.request_mark_price(nautilus_pyo3.InstrumentId.from_str(instrument_id))
        return mark

    async def orders(self) -> Sequence[OrderView]:
        """Kraken's open orders."""
        return [report_order_view(r) for r in await self._reports.order_reports()]

    async def positions(self) -> Sequence[PositionView]:
        """Kraken's open positions."""
        reported = [report_position_view(r) for r in await self._reports.position_reports()]
        return [p for p in reported if p is not None]

    async def buy(self, instrument_id: str, client_order_id: str, quantity: Decimal, price: Decimal | None) -> None:
        """A post only limit buy at ``price``, or a market buy when it is None."""
        await self._load()
        await self._http.submit_order(
            self._account_id,
            nautilus_pyo3.InstrumentId.from_str(instrument_id),
            nautilus_pyo3.ClientOrderId(client_order_id),
            nautilus_pyo3.OrderSide.BUY,
            nautilus_pyo3.OrderType.MARKET if price is None else nautilus_pyo3.OrderType.LIMIT,
            nautilus_pyo3.Quantity.from_str(str(quantity)),
            nautilus_pyo3.TimeInForce.GTC,
            price=None if price is None else nautilus_pyo3.Price.from_str(str(price)),
            post_only=price is not None,
        )

    async def sell_stop(self, instrument_id: str, client_order_id: str, quantity: Decimal, trigger: Decimal) -> None:
        """A reduce only stop market sell triggered at ``trigger``."""
        await self._load()
        await self._http.submit_order(
            self._account_id,
            nautilus_pyo3.InstrumentId.from_str(instrument_id),
            nautilus_pyo3.ClientOrderId(client_order_id),
            nautilus_pyo3.OrderSide.SELL,
            nautilus_pyo3.OrderType.STOP_MARKET,
            nautilus_pyo3.Quantity.from_str(str(quantity)),
            nautilus_pyo3.TimeInForce.GTC,
            trigger_price=nautilus_pyo3.Price.from_str(str(trigger)),
            reduce_only=True,
        )

    async def cancel(self, order: OrderView) -> None:
        """Cancel one order by its venue order id."""
        await self._reports.cancel(order)


def foreign_id(now_ns: int) -> str:
    """A client order id the trader has never seen, ``drill-foreign-<ns>``."""
    return f"{FOREIGN_PREFIX}{now_ns}"


async def _size(kraken: DrillKraken, instrument_id: str) -> tuple[Spec, Decimal]:
    spec = await kraken.spec(instrument_id)
    return spec, drill_size(spec.size_increment, spec.min_quantity)


async def foreign_order(
    kraken: DrillKraken, settings: ReconcileSettings, now_ns: int, instrument_id: str = BTC_PERP
) -> dict[str, object]:
    """Rest a post only buy of the drill size far below the mark (D4)."""
    spec, size = await _size(kraken, instrument_id)
    price = rest_price(await kraken.mark(instrument_id), settings.drill_rest_distance_pct, spec.price_increment)
    client_order_id = foreign_id(now_ns)
    await kraken.buy(instrument_id, client_order_id, size, price)
    return {"sent": "limit_buy", "client_order_id": client_order_id, "quantity": str(size), "price": str(price)}


async def foreign_position(kraken: DrillKraken, now_ns: int, instrument_id: str = BTC_PERP) -> dict[str, object]:
    """Buy the drill size at market (D7); ``DrillRefused`` while Kraken already holds a position there."""
    held = [p for p in await kraken.positions() if p.instrument_id == instrument_id and p.quantity > 0]
    if held:
        raise DrillRefused(f"Kraken already holds a position on {instrument_id}: flatten it first")
    _, size = await _size(kraken, instrument_id)
    client_order_id = foreign_id(now_ns)
    await kraken.buy(instrument_id, client_order_id, size, None)
    return {"sent": "market_buy", "client_order_id": client_order_id, "quantity": str(size)}


async def _resting_stops(kraken: DrillKraken, instrument_id: str) -> list[OrderView]:
    return [
        o
        for o in await kraken.orders()
        if o.instrument_id == instrument_id and o.reduce_only and o.order_type in STOP_TYPES
    ]


async def cancel_stop(kraken: DrillKraken, instrument_id: str = BTC_PERP) -> dict[str, object]:
    """Cancel every reduce only stop resting on ``instrument_id`` (D6); ``DrillRefused`` when there is none."""
    stops = await _resting_stops(kraken, instrument_id)
    if not stops:
        raise DrillRefused(f"no reduce only stop rests on {instrument_id}")
    for stop in stops:
        await kraken.cancel(stop)
    return {"cancelled": [_order(o) for o in stops]}


async def extra_stop(
    kraken: DrillKraken, settings: ReconcileSettings, now_ns: int, instrument_id: str = BTC_PERP
) -> dict[str, object]:
    """Rest a second reduce only sell stop for Kraken's whole long position, ``DRILL_STOP_PCT`` below the lowest
    stop already there (D12); ``DrillRefused`` without a long position or without a stop."""
    held = [
        p
        for p in await kraken.positions()
        if p.instrument_id == instrument_id and p.side is PositionSide.LONG and p.quantity > 0
    ]
    if not held:
        raise DrillRefused(f"Kraken holds no long position on {instrument_id}: run the open_position step first")
    triggers = [o.trigger_price for o in await _resting_stops(kraken, instrument_id) if o.trigger_price is not None]
    if not triggers:
        raise DrillRefused(f"no reduce only stop rests on {instrument_id} to add a second one below")
    spec = await kraken.spec(instrument_id)
    trigger = stop_trigger(min(triggers), settings.drill_stop_pct, spec.price_increment)
    client_order_id = foreign_id(now_ns)
    await kraken.sell_stop(instrument_id, client_order_id, held[0].quantity, trigger)
    return {
        "sent": "stop_market_sell",
        "client_order_id": client_order_id,
        "quantity": str(held[0].quantity),
        "trigger_price": str(trigger),
    }


async def show(kraken: DrillKraken) -> dict[str, object]:
    """Kraken's open orders and positions."""
    return {
        "orders": [_order(o) for o in await kraken.orders()],
        "positions": [
            {"instrument_id": p.instrument_id, "side": p.side.value, "quantity": str(p.quantity)}
            for p in await kraken.positions()
        ],
    }


def _order(order: OrderView) -> dict[str, object]:
    return {
        "client_order_id": order.client_order_id,
        "venue_order_id": order.venue_order_id,
        "instrument_id": order.instrument_id,
        "side": order.side.value,
        "order_type": order.order_type,
        "quantity": str(order.quantity),
        "trigger_price": None if order.trigger_price is None else str(order.trigger_price),
        "reduce_only": order.reduce_only,
    }


async def run(
    action: str, kraken: DrillKraken, settings: ReconcileSettings, now_ns: Callable[[], int] = time.time_ns
) -> dict[str, object]:
    """Run ``action``; ``DrillRefused`` unless the trader is live in ``LIVE_MODE=drill``."""
    if not settings.drill_mode:
        raise DrillRefused("trader.drill runs only with TRADING_ENV=live and LIVE_MODE=drill")
    if action == "foreign-order":
        return await foreign_order(kraken, settings, now_ns())
    if action == "foreign-position":
        return await foreign_position(kraken, now_ns())
    if action == "cancel-stop":
        return await cancel_stop(kraken)
    if action == "extra-stop":
        return await extra_stop(kraken, settings, now_ns())
    if action == "show":
        return await show(kraken)
    raise DrillRefused(USAGE)


def kraken_client(secrets: Mapping[str, SecretStr], timeout_s: int) -> PyO3DrillKraken:
    """The drill's own Kraken Futures HTTP client, keys from the secret files, no retries."""
    return PyO3DrillKraken(
        KrakenFuturesHttpClient(
            api_key=secrets[KRAKEN_API_KEY].get_secret_value(),
            api_secret=secrets[KRAKEN_API_SECRET].get_secret_value(),
            base_url=None,
            timeout_secs=timeout_s,
            max_retries=0,
        )
    )


def drill_secrets(settings: ReconcileSettings) -> tuple[str, ...]:
    """The Kraken key files, only in drill mode."""
    return (KRAKEN_API_KEY, KRAKEN_API_SECRET) if settings.drill_mode else ()


def main(argv: list[str] | None = None) -> None:
    """Run one action and print its JSON line; exit 1 when refused or Kraken fails, 2 on a wrong command."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ACTIONS:
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    try:
        early = ReconcileSettings()
    except ValidationError:
        early = None  # boot logs why and exits 1
    if early is not None and not early.drill_mode:
        # Before boot: no key file read, no clock check.
        print("refused: trader.drill runs only with TRADING_ENV=live and LIVE_MODE=drill", file=sys.stderr)
        sys.exit(1)
    started = boot("node", settings_cls=ReconcileSettings, secrets=drill_secrets)
    kraken = kraken_client(started.secrets, started.settings.reconcile_fetch_timeout_s)
    try:
        result = asyncio.run(run(args[0], kraken, started.settings))
    except DrillRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:  # Kraken said no or was unreachable: say so, never a traceback with the request
        print(json.dumps({"status": "error", "error": type(exc).__name__, "detail": str(exc)}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()

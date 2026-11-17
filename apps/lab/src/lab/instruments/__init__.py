"""The pinned instrument definitions (spec 0008, *Pinned instrument*), loaded by ``lab.instruments``.

Each ``<instrument id>.json`` is a ``CryptoPerpetual.to_dict`` with the real fees written in by
hand. Keys starting with ``_`` are not part of the instrument and the loader drops them: ``_note``, and
the fetch rules ``_tick_history`` and ``_drop_spans`` (spec 0033 AC-9a), read by ``fetch_rules``.
"""

import json
from decimal import Decimal
from importlib import resources

from nautilus_trader.model.instruments import CryptoPerpetual

from trading_core.lab.candles import FetchRules, parse_fetch_rules


class UnknownInstrumentError(LookupError):
    """No pinned definition exists for the instrument id."""


def _file(instrument_id: str) -> dict[str, object]:
    resource = resources.files(__name__) / f"{instrument_id}.json"
    if "/" in instrument_id or "\\" in instrument_id or instrument_id.startswith(".") or not resource.is_file():
        raise UnknownInstrumentError(f"no pinned instrument for {instrument_id}")
    raw: dict[str, object] = json.loads(resource.read_text(encoding="utf-8"))
    return raw


def _definition(instrument_id: str) -> dict[str, object]:
    return {key: value for key, value in _file(instrument_id).items() if not key.startswith("_")}


def fetch_rules(instrument_id: str) -> FetchRules:
    """The pin's tick history and dropped spans for ``fetch-candles`` (spec 0033 AC-9a); empty when it has
    neither. Raises ``FetchRulesError`` on a malformed entry."""
    raw = _file(instrument_id)
    return parse_fetch_rules(raw.get("_tick_history"), raw.get("_drop_spans"))


def is_pinned(instrument_id: str) -> bool:
    """Whether a pinned definition exists for the instrument id."""
    try:
        _definition(instrument_id)
    except UnknownInstrumentError:
        return False
    return True


def pinned_instrument(instrument_id: str) -> CryptoPerpetual:
    """The pinned instrument, fees as written in the file."""
    return CryptoPerpetual.from_dict(_definition(instrument_id))


def on_tick(instrument_id: str, tick: Decimal) -> CryptoPerpetual:
    """A copy of the pinned instrument on an earlier finer tick from its ``_tick_history``, so ``fetch-candles``
    stores that month's prices at the tick of their time (spec 0033 AC-9a), not rounded to the pinned one."""
    precision = max(0, -int(tick.normalize().as_tuple().exponent))
    definition = _definition(instrument_id) | {"price_increment": str(tick), "price_precision": precision}
    return CryptoPerpetual.from_dict(definition)


def with_fees(instrument_id: str, maker_fee: Decimal, taker_fee: Decimal) -> CryptoPerpetual:
    """A copy of the pinned instrument carrying the run's fees, for the engine (AC-5).

    ``MakerTakerFeeModel`` reads its fees from the instrument; the ``instruments`` row keeps the
    pinned values.
    """
    definition = _definition(instrument_id) | {"maker_fee": str(maker_fee), "taker_fee": str(taker_fee)}
    return CryptoPerpetual.from_dict(definition)

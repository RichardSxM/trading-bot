"""The pinned instrument definitions (spec 0008, *Pinned instrument*), loaded by ``lab.instruments``.

Each ``<instrument id>.json`` is a ``CryptoPerpetual.to_dict`` with the real fees written in by
hand and a ``_note`` key the loader drops.
"""

import json
from decimal import Decimal
from importlib import resources

from nautilus_trader.model.instruments import CryptoPerpetual


class UnknownInstrumentError(LookupError):
    """No pinned definition exists for the instrument id."""


def _definition(instrument_id: str) -> dict[str, object]:
    resource = resources.files(__name__) / f"{instrument_id}.json"
    if "/" in instrument_id or "\\" in instrument_id or instrument_id.startswith(".") or not resource.is_file():
        raise UnknownInstrumentError(f"no pinned instrument for {instrument_id}")
    raw: dict[str, object] = json.loads(resource.read_text(encoding="utf-8"))
    raw.pop("_note", None)
    return raw


def pinned_instrument(instrument_id: str) -> CryptoPerpetual:
    """The pinned instrument, fees as written in the file."""
    return CryptoPerpetual.from_dict(_definition(instrument_id))


def with_fees(instrument_id: str, maker_fee: Decimal, taker_fee: Decimal) -> CryptoPerpetual:
    """A copy of the pinned instrument carrying the run's fees, for the engine (AC-5).

    ``MakerTakerFeeModel`` reads its fees from the instrument; the ``instruments`` row keeps the
    pinned values.
    """
    definition = _definition(instrument_id) | {"maker_fee": str(maker_fee), "taker_fee": str(taker_fee)}
    return CryptoPerpetual.from_dict(definition)

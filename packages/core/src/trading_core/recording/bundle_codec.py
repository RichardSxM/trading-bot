"""The ``.bundle.json`` file of a lab run (spec 0008 AC-11, spec 0015 AC-7): encode and parse.

The lab writes the exact bundle it wrote into its own Postgres; ``gate import`` parses it on the VPS
into the same ``RunBundle``. Decimals are fixed notation strings, ids strings, times ISO, enums their
values, keys sorted. Parsing rebuilds every row through its dataclass, so the row checks run again
(no float, UTC times only), and an ``_at`` field derived from its ``_ns`` sibling is derived again,
never read. Older bundles without the spec 0015 parts still parse.
"""

import dataclasses
import json
import types
import typing
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Final, TypeAliasType, cast
from uuid import UUID

from trading_core.recording.rows import RunBundle


class BundleFormatError(ValueError):
    """A bundle file that is not valid JSON or does not match the bundle's shape."""


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def bundle_json(bundle: RunBundle) -> str:
    """The exact bundle written to Postgres, as JSON with sorted keys."""
    return json.dumps(dataclasses.asdict(bundle), indent=2, sort_keys=True, default=_plain) + "\n"


_SCALARS: Final = (str, int, bool)


def _decode(kind: object, value: object, where: str) -> object:
    """``value`` from JSON as ``kind`` (a row field's annotation)."""
    if isinstance(kind, TypeAliasType):
        return value  # JSON content: kept as parsed, the row normalizes it
    origin = typing.get_origin(kind)
    if origin in (types.UnionType, typing.Union):
        options = typing.get_args(kind)
        if value is None:
            if type(None) in options:
                return None
            raise BundleFormatError(f"{where} may not be null")
        real = [o for o in options if o is not type(None)]
        if len(real) != 1:
            raise BundleFormatError(f"{where}: ambiguous type {kind!r}")
        return _decode(real[0], value, where)
    if origin in (Sequence, list, tuple):
        if not isinstance(value, list):
            raise BundleFormatError(f"{where} must be a list")
        (item,) = typing.get_args(kind)[:1]
        return tuple(_decode(item, v, f"{where}[{i}]") for i, v in enumerate(value))
    if isinstance(kind, type) and dataclasses.is_dataclass(kind):
        return _row(kind, value, where)
    if isinstance(kind, type) and issubclass(kind, Enum):
        try:
            return kind(value)
        except ValueError as exc:
            raise BundleFormatError(f"{where}: {value!r} is not a {kind.__name__}") from exc
    if kind is Decimal:
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise BundleFormatError(f"{where} must be a decimal string")
        return Decimal(value)
    if kind is UUID:
        return UUID(str(value))
    if kind is datetime:
        return datetime.fromisoformat(str(value))
    if kind is date:
        return date.fromisoformat(str(value))
    if kind in _SCALARS:
        if type(value) is not kind:
            raise BundleFormatError(f"{where} must be {cast(type, kind).__name__}, not {value!r}")
        return value
    raise BundleFormatError(f"{where}: unsupported type {kind!r}")


def _row[T](cls: type[T], value: object, where: str) -> T:
    if not isinstance(value, Mapping):
        raise BundleFormatError(f"{where} must be an object")
    hints = typing.get_type_hints(cls)
    values: dict[str, object] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]  # cls is a dataclass here
        if not f.init:
            continue  # derived (an ``_at`` from its ``_ns``)
        if f.name not in value:
            if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise BundleFormatError(f"{where}.{f.name} is missing")
            continue
        values[f.name] = _decode(hints[f.name], value[f.name], f"{where}.{f.name}")
    try:
        return cls(**values)
    except (TypeError, ValueError) as exc:
        raise BundleFormatError(f"{where}: {exc}") from exc


def parse_bundle(text: str) -> RunBundle:
    """A ``RunBundle`` from ``bundle_json`` output; raises ``BundleFormatError`` for anything else."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"not JSON: {exc}") from exc
    return _row(RunBundle, data, "bundle")

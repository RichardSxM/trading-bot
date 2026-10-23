"""The ``.bundle.json`` file of a lab run (spec 0008 AC-11, spec 0015 AC-7): encode and parse.

The lab writes the exact bundle it wrote into its own Postgres; ``gate import`` parses it on the VPS
into the same ``RunBundle``. Decimals are fixed notation strings, ids strings, times ISO, enums their
values, keys sorted. Parsing rebuilds every row through its dataclass, so the row checks run again
(no float, UTC times only), and an ``_at`` field derived from its ``_ns`` sibling is derived again,
never read. Older bundles without the spec 0015 parts still parse. ``BundleFile`` reads a file in
parts for ``gate import``, so a large bundle is never held whole.
"""

import dataclasses
import json
import types
import typing
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Final, TypeAliasType, cast
from uuid import UUID

import ijson

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
_PARSERS: Final[dict[object, Callable[[str], object]]] = {
    UUID: UUID,
    datetime: datetime.fromisoformat,
    date: date.fromisoformat,
}


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
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise BundleFormatError(f"{where}: {value!r} is not a decimal") from exc
    if kind in _PARSERS:
        try:
            return _PARSERS[kind](str(value))
        except ValueError as exc:
            raise BundleFormatError(f"{where}: {value!r} is not a {cast(type, kind).__name__}") from exc
    if kind in _SCALARS:
        if type(value) is not kind:
            raise BundleFormatError(f"{where} must be {cast(type, kind).__name__}, not {value!r}")
        return value
    raise BundleFormatError(f"{where}: unsupported type {kind!r}")


_FIELDS: Final[dict[type, tuple[tuple[str, object, bool], ...]]] = {}


def _fields(cls: type) -> tuple[tuple[str, object, bool], ...]:
    """Each init field of a row class: its name, annotation, and whether it must be present.

    Kept per class: ``get_type_hints`` on every row was a third of a large import's time.
    """
    if (known := _FIELDS.get(cls)) is not None:
        return known
    hints = typing.get_type_hints(cls)
    _FIELDS[cls] = tuple(
        (f.name, hints[f.name], f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING)
        for f in dataclasses.fields(cls)
        if f.init  # a derived field (an ``_at`` from its ``_ns``) is never read
    )
    return _FIELDS[cls]


def _row[T](cls: type[T], value: object, where: str) -> T:
    if not isinstance(value, Mapping):
        raise BundleFormatError(f"{where} must be an object")
    values: dict[str, object] = {}
    for name, kind, required in _fields(cls):
        if name not in value:
            if required:
                raise BundleFormatError(f"{where}.{name} is missing")
            continue
        values[name] = _decode(kind, value[name], f"{where}.{name}")
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


def _row_lists() -> dict[str, type]:
    """The bundle's row list parts, each with its row class."""
    hints = typing.get_type_hints(RunBundle)
    return {
        f.name: typing.get_args(hints[f.name])[0]
        for f in dataclasses.fields(RunBundle)
        if typing.get_origin(hints[f.name]) is Sequence
    }


_ROW_LISTS: Final = _row_lists()


class BundleFile:
    """A ``.bundle.json`` file read in parts, so a large replay bundle never sits in memory whole.

    A replay study's bundle can be hundreds of MB; ``parse_bundle`` on it needs several GB, more than
    the gate's container has. ``head`` returns every part but the row lists (left empty), and ``rows``
    streams one row list a row at a time. Each call reads the file again from the start. Numbers parse
    as ``json.loads`` parses them, so the rows see the same values ``parse_bundle`` gives.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def head(self) -> RunBundle:
        """The bundle without its row lists; raises ``BundleFormatError`` for a malformed file."""
        parts: dict[str, object] = {}
        key: str | None = None
        builder: ijson.common.ObjectBuilder | None = None
        try:
            with self.path.open("rb") as f:
                for prefix, event, value in ijson.parse(f, use_float=True):
                    if prefix == "":
                        if builder is not None and key is not None:
                            parts[key] = builder.value
                        builder = None
                        if event == "map_key":
                            key = value
                            if key not in _ROW_LISTS:
                                builder = ijson.common.ObjectBuilder()
                        elif event not in ("start_map", "end_map"):
                            raise BundleFormatError("bundle must be an object")
                    elif builder is not None:
                        builder.event(event, value)
                    elif prefix == key and event not in ("start_array", "end_array"):
                        raise BundleFormatError(f"bundle.{key} must be a list")
        except ijson.JSONError as exc:
            raise BundleFormatError(f"not JSON: {exc}") from exc
        return _row(RunBundle, parts, "bundle")

    def rows[T](self, part: str, cls: type[T]) -> Iterator[T]:
        """Each row of the ``part`` list as a ``cls``; raises ``BundleFormatError`` for a malformed row."""
        if _ROW_LISTS.get(part) is not cls:
            raise ValueError(f"bundle part {part} does not hold {cls.__name__} rows")
        try:
            with self.path.open("rb") as f:
                for i, value in enumerate(ijson.items(f, f"{part}.item", use_float=True)):
                    yield _row(cls, value, f"bundle.{part}[{i}]")
        except ijson.JSONError as exc:
            raise BundleFormatError(f"not JSON: {exc}") from exc

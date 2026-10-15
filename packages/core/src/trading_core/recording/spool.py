"""The JSON lines codec for queued writes (spec 0006 AC-12, AC-13).

One line per write: ``{"call": <repository function>, "row_type": <row class>, "row": {...}}``.
``Decimal``, ``UUID``, and datetimes are strings, ``StrEnum`` members their values, ints ints.
Derived ``_at`` fields are left out (the row rebuilds them from ``_ns``), so a decoded row
equals the one encoded. Spill files hold what the writer could not write before a stop and are
replayed at the next start; dead letter files hold what failed and are only for you to read.
"""

import dataclasses
import json
import logging
import types
import typing
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Final, Union, get_args, get_origin, get_type_hints
from uuid import UUID

from trading_core.recording.rows import (
    AccountSnapshotRow,
    DecisionRow,
    FillRow,
    FundingRow,
    InstrumentRow,
    OrderEventRow,
    OrderRow,
    PositionRow,
    PositionSnapshotRow,
    RiskEventRow,
)

log = logging.getLogger(__name__)

type QueuedRow = (
    InstrumentRow
    | DecisionRow
    | OrderRow
    | OrderEventRow
    | FillRow
    | PositionRow
    | PositionSnapshotRow
    | AccountSnapshotRow
    | RiskEventRow
    | FundingRow
)


class WriteCall(StrEnum):
    """The repository function a queued row goes through."""

    UPSERT_INSTRUMENT = "upsert_instrument"
    RECORD_DECISION = "record_decision"
    UPSERT_ORDER = "upsert_order"
    RECORD_ORDER_EVENT = "record_order_event"
    RECORD_FILL = "record_fill"
    UPSERT_POSITION = "upsert_position"
    RECORD_POSITION_SNAPSHOT = "record_position_snapshot"
    RECORD_ACCOUNT_SNAPSHOT = "record_account_snapshot"
    RECORD_RISK_EVENT = "record_risk_event"
    RECORD_FUNDING = "record_funding"


ROW_TYPES: Final[dict[WriteCall, type[QueuedRow]]] = {
    WriteCall.UPSERT_INSTRUMENT: InstrumentRow,
    WriteCall.RECORD_DECISION: DecisionRow,
    WriteCall.UPSERT_ORDER: OrderRow,
    WriteCall.RECORD_ORDER_EVENT: OrderEventRow,
    WriteCall.RECORD_FILL: FillRow,
    WriteCall.UPSERT_POSITION: PositionRow,
    WriteCall.RECORD_POSITION_SNAPSHOT: PositionSnapshotRow,
    WriteCall.RECORD_ACCOUNT_SNAPSHOT: AccountSnapshotRow,
    WriteCall.RECORD_RISK_EVENT: RiskEventRow,
    WriteCall.RECORD_FUNDING: FundingRow,
}
"""The row type each call takes."""

_BY_NAME: Final = {cls.__name__: cls for cls in ROW_TYPES.values()}


class SpoolDecodeError(ValueError):
    """A spool line is not a queued write this codec wrote."""


@dataclass(frozen=True)
class WriteItem:
    """One queued write: the repository call and its row."""

    call: WriteCall
    row: QueuedRow

    def __post_init__(self) -> None:
        if not isinstance(self.row, ROW_TYPES[self.call]):
            raise TypeError(f"{self.call} takes {ROW_TYPES[self.call].__name__}, not {type(self.row).__name__}")


def _plain(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, bool | int | str) or value is None:
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    raise TypeError(f"{type(value).__name__} cannot go into a spool line")


def encode(item: WriteItem) -> str:
    """One JSON line (no newline) for ``item``."""
    row = {
        f.name: _plain(getattr(item.row, f.name))
        for f in dataclasses.fields(item.row)
        if f.init  # derived _at fields are rebuilt from _ns
    }
    return json.dumps({"call": item.call.value, "row_type": type(item.row).__name__, "row": row}, sort_keys=True)


def _typed(value: object, hint: object) -> object:
    if value is None:
        return None
    origin = get_origin(hint)
    if origin in (Union, types.UnionType):
        (inner,) = [arg for arg in get_args(hint) if arg is not type(None)]
        return _typed(value, inner)
    if isinstance(hint, typing.TypeAliasType):  # JsonObject: kept as plain JSON
        return value
    if hint is Decimal:
        return Decimal(typing.cast(str, value))
    if hint is UUID:
        return UUID(typing.cast(str, value))
    if hint is datetime:
        return datetime.fromisoformat(typing.cast(str, value))
    if isinstance(hint, type) and issubclass(hint, StrEnum):
        return hint(typing.cast(str, value))
    return value


def decode(line: str) -> WriteItem:
    """The ``WriteItem`` a line from ``encode`` holds; raises ``SpoolDecodeError`` on anything else."""
    try:
        data = json.loads(line)
        call = WriteCall(data["call"])
        cls = _BY_NAME[data["row_type"]]
        hints = get_type_hints(cls)
        fields = {f.name for f in dataclasses.fields(cls) if f.init}
        values = {name: _typed(value, hints[name]) for name, value in data["row"].items() if name in fields}
        return WriteItem(call, typing.cast(Callable[..., QueuedRow], cls)(**values))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise SpoolDecodeError(f"not a spool line: {exc}") from exc


def append(path: Path, items: list[WriteItem]) -> None:
    """Append ``items`` to ``path``, one line each, and flush them to disk."""
    if not items:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        for item in items:
            file.write(encode(item) + "\n")
        file.flush()


def read(path: Path) -> Iterator[WriteItem]:
    """The items in a spool file, in order. A truncated last line is skipped with a WARNING.

    Any other bad line raises ``SpoolDecodeError``: the file was not written by this codec.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    complete, last = lines[:-1], lines[-1]
    for line in complete:
        if line:
            yield decode(line)
    if last:
        log.warning(
            "truncated last spool line skipped",
            extra={"fields": {"code": "spool_truncated_line", "path": str(path)}},
        )

"""The paper CSV of the parity check (spec 0008 AC-15, *Value sourcing*).

``scripts/fetch-paper-decisions.sh`` copies the paper runs and decisions of ``placeholder_timed``
out of the VPS Postgres, then pipes them through ``python -m lab.paper_csv``, which writes each
run's config in ``canonical_json`` form with its sha256. So both sides of the config comparison
hash the same text, made by the same function. ``read_paper_csv`` reads the finished file.

One ``kind`` per row: ``decision`` (``run_id``, ``decided_ns``, ``intent``, ``side``,
``risk_verdict``, ``risk_reason``) and ``run`` (``run_id``, ``started_at``, ``ended_at``,
``config``, ``config_sha256``). Times are UTC ISO 8601 with a ``Z``.
"""

import csv
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TextIO

from trading_core.lab.parity import Decided, PaperRun, ParityInputError, config_sha256
from trading_core.recording.enums import RiskVerdict
from trading_core.recording.rows import JsonObject, canonical_json

COLUMNS: Final = (
    "kind",
    "run_id",
    "decided_ns",
    "intent",
    "side",
    "risk_verdict",
    "risk_reason",
    "started_at",
    "ended_at",
    "config",
    "config_sha256",
)
DECISION: Final = "decision"
RUN: Final = "run"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class PaperDecision:
    """A paper decision and the run it belongs to."""

    run_id: str
    decided: Decided


@dataclass(frozen=True)
class PaperData:
    """Everything the CSV holds."""

    runs: tuple[PaperRun, ...]
    decisions: tuple[PaperDecision, ...]


def utc_ns(text: str) -> int:
    """Nanoseconds since the epoch of an ISO 8601 UTC time; ``ValueError`` without an offset of zero."""
    moment = datetime.fromisoformat(text)
    if moment.utcoffset() != timedelta(0):
        raise ValueError(f"{text!r} is not UTC")
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def _config(text: str) -> JsonObject:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("config is not a JSON object")
    config: JsonObject = value
    return config


def _row(row: dict[str, str]) -> PaperRun | PaperDecision:
    kind = row["kind"]
    if kind == RUN:
        config = _config(row["config"])
        if row["config"] != canonical_json(config):
            raise ValueError("config is not in canonical_json form")
        if row["config_sha256"] != config_sha256(config):
            raise ValueError("config_sha256 does not match config")
        return PaperRun(
            run_id=row["run_id"],
            started_ns=utc_ns(row["started_at"]),
            ended_ns=utc_ns(row["ended_at"]),
            config=config,
            config_sha256=row["config_sha256"],
        )
    if kind == DECISION:
        verdict = RiskVerdict(row["risk_verdict"])
        return PaperDecision(
            run_id=row["run_id"],
            decided=Decided(
                decided_ns=int(row["decided_ns"]),
                intent=row["intent"],
                side=row["side"] or None,
                approved=verdict == RiskVerdict.APPROVED,
                risk_reason=row["risk_reason"] or None,
            ),
        )
    raise ValueError(f"unknown kind {kind!r}")


def read_paper_csv(path: Path) -> PaperData:
    """Read and check the finished paper CSV.

    Raises ``ParityInputError`` listing every bad row (wrong header, unknown kind, a bad number or
    time, a config not in canonical form, or a hash that does not match its config).
    """
    try:
        handle = path.open(newline="", encoding="utf-8")
    except OSError as exc:
        raise ParityInputError([f"{path}: cannot read ({exc.strerror})"]) from None
    runs: list[PaperRun] = []
    decisions: list[PaperDecision] = []
    problems: list[str] = []
    with handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ParityInputError([f"{path}: header must be {','.join(COLUMNS)}"])
        for n, row in enumerate(reader, start=2):
            try:
                parsed = _row(row)
            except (ValueError, KeyError, TypeError) as exc:
                problems.append(f"{path}:{n}: {exc}")
                continue
            if isinstance(parsed, PaperRun):
                runs.append(parsed)
            else:
                decisions.append(parsed)
    if problems:
        raise ParityInputError(problems)
    return PaperData(runs=tuple(runs), decisions=tuple(decisions))


def finish_export(rows: Iterable[dict[str, str]], out: TextIO) -> int:
    """Write the export's rows as the finished CSV: each run's config canonical, with its sha256.

    Returns how many rows were written. Raises ``ValueError`` for a run row whose config is not a
    JSON object (or holds a float, which ``canonical_json`` refuses).
    """
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    count = 0
    for row in rows:
        finished = {column: row.get(column) or "" for column in COLUMNS}
        if finished["kind"] == RUN:
            config = _config(finished["config"])
            finished["config"] = canonical_json(config)
            finished["config_sha256"] = config_sha256(config)
        writer.writerow(finished)
        count += 1
    return count


def main() -> None:
    """``python -m lab.paper_csv``: the raw export on stdin, the finished CSV on stdout."""
    reader = csv.DictReader(sys.stdin)
    if tuple(reader.fieldnames or ()) != COLUMNS:
        print(f"paper_csv: the export header must be {','.join(COLUMNS)}", file=sys.stderr)
        sys.exit(1)
    count = finish_export(reader, sys.stdout)
    print(f"paper_csv: {count} rows", file=sys.stderr)


if __name__ == "__main__":
    main()

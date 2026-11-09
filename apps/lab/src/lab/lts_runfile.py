"""The liquidity trap pre check's run file (spec 0031 AC-16).

It is frozen: committed before the first run and never changed afterward. Days are native TOML dates.
Every problem is collected, and the command exits 64 listing them all.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lab.instruments import is_pinned
from lab.runfile import DecimalStr, RunFileError, read_toml

NativeDate = Annotated[date, Field(strict=True)]


class LtsRunFile(BaseModel):
    """Every setting of one pre check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument_id: str
    lookback_s: int = Field(gt=0)
    sweep_buffer_ticks: int = Field(ge=0)
    reclaim_buffer_ticks: int = Field(ge=0)
    invalidation_buffer_ticks: int = Field(ge=0)
    reclaim_timeout_ms: int = Field(gt=0)
    efficiency_pct_min: int = Field(ge=0, le=100)
    horizon_s: int = Field(gt=0)
    depth_band_bps: int = Field(gt=0)
    maker_fee: DecimalStr = Field(ge=0, lt=1)
    taker_fee: DecimalStr = Field(ge=0, lt=1)
    hurdle_k: DecimalStr = Field(gt=0)
    min_signals_per_day: int = Field(gt=0)
    min_positive_days: int = Field(gt=0)
    days: list[NativeDate] = Field(min_length=1)


@dataclass(frozen=True)
class LoadedLtsRun:
    """A valid run file and its hash."""

    run: LtsRunFile
    file_hash: str


def _rule_problems(run: LtsRunFile, holdout_start: date) -> list[str]:
    problems: list[str] = []
    if not is_pinned(run.instrument_id):
        problems.append(f"instrument_id: no pinned definition for {run.instrument_id}")
    if run.days != sorted(set(run.days)):
        problems.append("days: must be sorted and unique")
    late = [day.isoformat() for day in run.days if day >= holdout_start]
    if late:
        problems.append(f"days: on or after holdout_start {holdout_start}: {', '.join(late)}")
    if run.min_positive_days > len(run.days):
        problems.append(f"min_positive_days: {run.min_positive_days} is more than the {len(run.days)} days")
    return problems


def load_lts_run(path: Path, holdout_start: date) -> LoadedLtsRun:
    """Read and validate the run file; ``RunFileError`` lists every problem."""
    data, raw = read_toml(path)
    try:
        run = LtsRunFile.model_validate(raw)
    except ValidationError as exc:
        raise RunFileError(
            [f"{'.'.join(str(part) for part in e['loc']) or 'run file'}: {e['msg']}" for e in exc.errors()]
        ) from exc
    problems = _rule_problems(run, holdout_start)
    if problems:
        raise RunFileError(problems)
    return LoadedLtsRun(run, hashlib.sha256(data).hexdigest())

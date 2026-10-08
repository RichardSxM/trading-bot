"""The run file (spec 0008, *Run file*): one TOML file says what to replay and how.

It is validated at startup, CLI overrides included, before anything is read or written. Every
problem is collected so a bad file is fixed in one pass (exit 64, AC-3).
"""

import hashlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from lab.strategies import StrategyConfigError, registry_entry


class RunFileError(Exception):
    """The run file or an override is invalid; ``problems`` lists every one."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _decimal_from_str(value: object) -> object:
    # Money and fees are strings in the file, so no float ever becomes a Decimal (AGENTS.md).
    if not isinstance(value, str):
        raise ValueError('must be a string, like "0.0005"')
    return value


def _finite(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("must be a finite number")
    return value


DecimalStr = Annotated[Decimal, BeforeValidator(_decimal_from_str)]
# A [strategy.config] value: TOML integers, strings, and booleans only (no floats).
ConfigValue = int | str | bool


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskSection(_Section):
    """The ``RiskLimits`` of spec 0006's ``check_entry``, same field names."""

    max_order_notional: DecimalStr = Field(gt=0)
    max_leverage: DecimalStr = Field(gt=0)


class StrategySection(_Section):
    """Which registered strategy to run, and its own settings."""

    name: str
    config: dict[str, ConfigValue] = Field(default_factory=dict)


class RunFile(_Section):
    """A validated run file. ``from`` is a Python keyword, so the field is ``from_``."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    source: Literal["tardis", "own"]
    instrument_id: str
    from_: date = Field(alias="from")
    to: date
    seed: int
    maker_fee: DecimalStr
    taker_fee: DecimalStr
    latency_ms: int = Field(ge=0)
    starting_balance_usd: DecimalStr = Field(gt=0)
    leverage: DecimalStr = Field(gt=0)
    risk: RiskSection
    strategy: StrategySection

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("maker_fee", "taker_fee"):
            fee = _finite(getattr(self, name))
            if not Decimal(0) <= fee < 1:
                raise ValueError(f"{name} must be in [0, 1), got {fee}")
        if self.from_ > self.to:
            raise ValueError(f"from {self.from_} is after to {self.to}")
        return self


@dataclass(frozen=True)
class Overrides:
    """CLI values that replace run file keys before validation (AC-3)."""

    from_: date | None = None
    to: date | None = None
    seed: int | None = None

    def as_dict(self) -> dict[str, str | int]:
        """What changed, for ``run_config.overrides`` (dates as ISO strings)."""
        found: dict[str, str | int] = {}
        if self.from_ is not None:
            found["from"] = self.from_.isoformat()
        if self.to is not None:
            found["to"] = self.to.isoformat()
        if self.seed is not None:
            found["seed"] = self.seed
        return found


@dataclass(frozen=True)
class LoadedRun:
    """A run file that passed validation, with what identifies it."""

    path: str
    sha256: str
    run: RunFile
    overrides: Mapping[str, str | int] = field(default_factory=dict)


def _apply(raw: dict[str, object], overrides: Overrides) -> dict[str, object]:
    merged = dict(raw)
    if overrides.from_ is not None:
        merged["from"] = overrides.from_
    if overrides.to is not None:
        merged["to"] = overrides.to
    if overrides.seed is not None:
        merged["seed"] = overrides.seed
    return merged


def _location(loc: tuple[int | str, ...]) -> str:
    return ".".join("from" if part == "from_" else str(part) for part in loc) or "run file"


def load_run(path: Path, overrides: Overrides) -> LoadedRun:
    """Read, override, and validate a run file, including its strategy's own config.

    Raises ``RunFileError`` listing every problem found.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise RunFileError([f"{path}: cannot read ({exc.strerror})"]) from None
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RunFileError([f"{path}: not valid TOML ({exc})"]) from None
    try:
        run = RunFile.model_validate(_apply(raw, overrides))
    except ValidationError as exc:
        problems = [f"{_location(e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False, include_input=False)]
        raise RunFileError(problems) from None
    try:
        entry = registry_entry(run.strategy.name)
    except StrategyConfigError as exc:
        raise RunFileError(exc.problems) from None
    try:
        entry.check_config(run.strategy.config)
    except StrategyConfigError as exc:
        raise RunFileError([f"strategy.config: {p}" for p in exc.problems]) from None
    return LoadedRun(path=str(path), sha256=hashlib.sha256(data).hexdigest(), run=run, overrides=overrides.as_dict())

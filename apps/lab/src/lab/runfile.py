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
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, model_validator

from lab.strategies import MarketContextValues, StrategyConfigError, registry_entry
from trading_core.lab.parity import PaperStrategy, ParityInputError, paper_config
from trading_core.node.placeholder import STRATEGY_NAME as PLACEHOLDER
from trading_core.risk.check import RiskLimits
from trading_core.settings import PaperLoopSettings

# The paper loop's default stop accept timeout (spec 0006); the placeholder needs one in replay.
STOP_ACCEPT_TIMEOUT_S: Final[Decimal] = PaperLoopSettings.model_fields["stop_accept_timeout_s"].default


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
    """The ``RiskLimits`` of ``check_entry`` (spec 0006, spec 0011), same field names.

    The spec 0011 rules default to the trader's ``RISK_*`` defaults; ``max_total_exposure``
    defaults to ``max_order_notional`` (a replay holds one position at a time).
    """

    max_order_notional: DecimalStr = Field(gt=0)
    max_leverage: DecimalStr = Field(gt=0)
    max_total_exposure: DecimalStr | None = Field(default=None, gt=0)
    risk_per_trade: DecimalStr = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.02"))
    maintenance_margin_rate: DecimalStr = Field(default=Decimal("0.01"), gt=0, lt=Decimal("0.5"))
    min_liq_distance_pct: DecimalStr = Field(default=Decimal(10), gt=0, lt=100)
    liq_stop_multiple: DecimalStr = Field(default=Decimal(3), ge=1)
    stop_slippage_bps: DecimalStr = Field(default=Decimal(5), ge=0, le=500)

    def limits(self) -> RiskLimits:
        """This section as the guard's ``RiskLimits``."""
        return RiskLimits(
            max_order_notional=self.max_order_notional,
            max_leverage=self.max_leverage,
            max_total_exposure=self.max_order_notional if self.max_total_exposure is None else self.max_total_exposure,
            risk_per_trade=self.risk_per_trade,
            maintenance_margin_rate=self.maintenance_margin_rate,
            min_liq_distance_pct=self.min_liq_distance_pct,
            liq_stop_multiple=self.liq_stop_multiple,
            stop_slippage_bps=self.stop_slippage_bps,
        )


class ContextSection(_Section):
    """The market context a replayed strategy reads (spec 0009 AC-13), with ``ContextSettings``' defaults.

    ``warmup_s`` is how far before each segment its context rows start.
    """

    warmup_s: int = Field(default=7_200, ge=0)
    max_age_mark_s: int = Field(default=30, ge=1)
    max_age_index_s: int = Field(default=30, ge=1)
    max_age_open_interest_s: int = Field(default=30, ge=1)
    max_age_funding_s: int = Field(default=7_200, ge=1)
    liquidation_window_s: int = Field(default=60, ge=1)

    def values(self, source: str) -> MarketContextValues:
        """The values a strategy is built with; only ``own`` data holds liquidations."""
        return MarketContextValues(
            max_age_mark_s=self.max_age_mark_s,
            max_age_index_s=self.max_age_index_s,
            max_age_open_interest_s=self.max_age_open_interest_s,
            max_age_funding_s=self.max_age_funding_s,
            liquidation_window_s=self.liquidation_window_s,
            liquidations_available=source == "own",
        )


class StrategySection(_Section):
    """Which registered strategy to run, and its own settings."""

    name: str
    config: dict[str, ConfigValue] = Field(default_factory=dict)


class RunFile(_Section):
    """A validated run file. ``from`` is a Python keyword, so the field is ``from_``.

    The parity run file gives ``[paper]`` (the trader's ``strategy_config()``, verbatim) in place
    of ``[risk]`` and ``[strategy.config]``, and only for ``placeholder_timed`` (AC-15).
    """

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
    risk: RiskSection | None = None
    strategy: StrategySection
    paper: dict[str, ConfigValue] | None = None
    context: ContextSection = Field(default_factory=ContextSection)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name in ("maker_fee", "taker_fee"):
            fee = _finite(getattr(self, name))
            if not Decimal(0) <= fee < 1:
                raise ValueError(f"{name} must be in [0, 1), got {fee}")
        if self.from_ > self.to:
            raise ValueError(f"from {self.from_} is after to {self.to}")
        if self.paper is None:
            if self.risk is None:
                raise ValueError("[risk] is missing")
            return self
        if self.strategy.name != PLACEHOLDER:
            raise ValueError(f'[paper] is only for strategy.name = "{PLACEHOLDER}"')
        if self.risk is not None or self.strategy.config:
            raise ValueError("[paper] replaces [risk] and [strategy.config]; remove them")
        return self

    def paper_strategy(self) -> PaperStrategy | None:
        """The ``[paper]`` table as the paper loop's settings; None without one."""
        return None if self.paper is None else paper_config(self.paper)

    def risk_limits(self) -> RiskLimits:
        """The ``check_entry`` limits, from ``[risk]`` or ``[paper]``."""
        paper = self.paper_strategy()
        if paper is not None:
            return paper.risk
        assert self.risk is not None  # the validator requires one of them
        return self.risk.limits()

    def strategy_values(self) -> dict[str, ConfigValue]:
        """The strategy's own settings: ``[strategy.config]``, or what ``[paper]`` gives the placeholder."""
        paper = self.paper_strategy()
        if paper is None:
            return dict(self.strategy.config)
        return {
            "interval_min": paper.interval_min,
            "hold_min": paper.hold_min,
            "lookback_min": paper.lookback_min,
            "stop_pct": str(paper.stop_pct),
            "stale_quote_s": str(paper.stale_quote_s),
            # Not in strategy_config(), so not in the hash; it never changes an entry decision.
            "stop_accept_timeout_s": str(STOP_ACCEPT_TIMEOUT_S),
        }

    def version_config(self) -> dict[str, ConfigValue]:
        """The replay's strategy version config: ``[paper]`` as the trader stores it, else ``[strategy.config]``."""
        return dict(self.strategy.config if self.paper is None else self.paper)


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


def load_run(path: Path, overrides: Overrides, *, test_strategies: bool = False) -> LoadedRun:
    """Read, override, and validate a run file, including its strategy's own config.

    ``test_strategies`` also accepts the test only strategies (``LAB_TEST_STRATEGIES=1``).
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
        entry = registry_entry(run.strategy.name, test_strategies=test_strategies)
    except StrategyConfigError as exc:
        raise RunFileError(exc.problems) from None
    try:
        values = run.strategy_values()
    except ParityInputError as exc:
        raise RunFileError([f"paper: {p}" for p in exc.problems]) from None
    try:
        entry.check_config(values)
    except StrategyConfigError as exc:
        raise RunFileError([f"strategy.config: {p}" for p in exc.problems]) from None
    return LoadedRun(path=str(path), sha256=hashlib.sha256(data).hexdigest(), run=run, overrides=overrides.as_dict())

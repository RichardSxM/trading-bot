"""The gate's thresholds (spec 0015), parsed from the packaged ``thresholds.toml`` bytes the caller passes in.

Stdlib only (AC-23), so plain frozen dataclasses checked by hand instead of a pydantic model. A study
stores ``to_json`` of the thresholds it was created with; ``from_json`` reads them back, so a study is
always judged by its own frozen copy, never by the file as it is today.
"""

import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import date
from decimal import Decimal
from typing import Final

SUPPORTED_VERSION: Final = 1


class ThresholdsError(ValueError):
    """The thresholds are missing a field, carry an unknown one, or hold a value out of range."""


@dataclass(frozen=True)
class CandleThresholds:
    """Fold layout and trade floor of a candle study (calendar months)."""

    first_in_sample_months: int
    step_months: int
    min_holdout_trades: int


@dataclass(frozen=True)
class ReplayThresholds:
    """Fold layout and trade floor of a replay study (counted in replayed days)."""

    first_in_sample_days: int
    step_days: int
    min_holdout_trades: int


@dataclass(frozen=True)
class PaperThresholds:
    """What the paper → live step needs (AC-16)."""

    min_days: int
    min_trades_scalping: int
    min_trades_hour_level: int
    fill_rate_tolerance: Decimal
    slippage_tolerance_bps: Decimal

    def trade_floor(self, strategy_kind: str) -> int:
        """The paper trade floor for this kind of strategy: hour level or scalping."""
        return self.min_trades_hour_level if strategy_kind == "hour_level" else self.min_trades_scalping


@dataclass(frozen=True)
class Thresholds:
    """Every threshold of one ``thresholds.toml`` version."""

    version: int
    holdout_start: date
    fee_hurdle_k: Decimal
    max_drawdown: Decimal
    pbo_max: Decimal
    cscv_blocks: int
    dsr_min: Decimal
    bootstrap_resamples: int
    bootstrap_min_block_days: int
    bootstrap_confidence: Decimal
    concentration_max_share: Decimal
    top_trades_quantile: Decimal
    candle: CandleThresholds
    replay: ReplayThresholds
    paper: PaperThresholds

    def to_json(self) -> dict[str, object]:
        """Plain JSON: Decimals as fixed notation strings, the date as ISO."""
        return _plain(asdict(self))  # type: ignore[return-value]  # a dict in, a dict out

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> Thresholds:
        """Thresholds from ``to_json`` output (or the TOML), validated; raises ``ThresholdsError``."""
        return _build(cls, data, "")


def _plain(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _coerce(kind: object, value: object, where: str) -> object:
    if kind in (int, "int"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ThresholdsError(f"{where} must be an integer, not {value!r}")
        if value < 1:
            raise ThresholdsError(f"{where} must be at least 1, not {value}")
        return value
    if kind in (Decimal, "Decimal"):
        if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
            raise ThresholdsError(f"{where} must be a number, not {value!r}")
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ThresholdsError(f"{where} must be a finite number of at least 0, not {value!r}")
        return number
    if kind in (date, "date"):
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ThresholdsError(f"{where} must be an ISO date, not {value!r}") from exc
        raise ThresholdsError(f"{where} must be a date, not {value!r}")
    raise ThresholdsError(f"{where}: unsupported field type {kind!r}")


_NESTED: Final[Mapping[str, type]] = {
    "candle": CandleThresholds,
    "replay": ReplayThresholds,
    "paper": PaperThresholds,
}


def _build[T](cls: type[T], data: Mapping[str, object], prefix: str) -> T:
    names = {f.name: f for f in fields(cls)}  # type: ignore[arg-type]  # every T here is a dataclass
    unknown = sorted(set(data) - set(names))
    missing = sorted(set(names) - set(data))
    if unknown or missing:
        raise ThresholdsError(f"{prefix or 'thresholds'}: missing {missing}, unknown {unknown}")
    values: dict[str, object] = {}
    for name, f in names.items():
        where = f"{prefix}{name}"
        value = data[name]
        if name in _NESTED and not prefix:
            if not isinstance(value, Mapping):
                raise ThresholdsError(f"{where} must be a table")
            values[name] = _build(_NESTED[name], value, f"{name}.")
        else:
            values[name] = _coerce(f.type, value, where)
    built = cls(**values)
    if isinstance(built, Thresholds):
        _check_ranges(built)
    return built


def _check_ranges(t: Thresholds) -> None:
    if t.version != SUPPORTED_VERSION:
        raise ThresholdsError(f"thresholds version {t.version} is not supported (only {SUPPORTED_VERSION})")
    for name in ("max_drawdown", "pbo_max", "dsr_min", "bootstrap_confidence", "concentration_max_share"):
        value = getattr(t, name)
        if not Decimal(0) < value < Decimal(1):
            raise ThresholdsError(f"{name} must be between 0 and 1 (exclusive), not {value}")
    if not Decimal(0) < t.top_trades_quantile <= Decimal(1):
        raise ThresholdsError(f"top_trades_quantile must be in (0, 1], not {t.top_trades_quantile}")
    if t.cscv_blocks < 2 or t.cscv_blocks % 2:
        raise ThresholdsError(f"cscv_blocks must be an even number of at least 2, not {t.cscv_blocks}")


def parse_thresholds(data: bytes) -> Thresholds:
    """Thresholds from TOML bytes (numbers with a fraction read as ``Decimal``); raises ``ThresholdsError``."""
    try:
        parsed = tomllib.loads(data.decode("utf-8"), parse_float=Decimal)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ThresholdsError(f"thresholds are not valid TOML: {exc}") from exc
    return Thresholds.from_json(parsed)

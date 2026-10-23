"""The lab's strategy registry (spec 0008, *Strategy registry*).

Each entry names the paper loop's own strategy class, its config class, and how the run's
values reach that config. A replay builds the strategy exactly as the trader does, with a
pause gate, entry blocks, and a ``record`` callable (AC-7); nothing here subclasses or patches
a strategy. The exceptions are the test only ``raise_probe`` and ``peek_probe`` (``lab.probes``),
which exist only when the caller asks for the test strategies (``LAB_TEST_STRATEGIES=1``, AC-13).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

import msgspec
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarSpecification
from nautilus_trader.model.enums import BarAggregation, PriceType

from lab.probes import PeekProbe, RaiseProbe, RaiseProbeConfig
from trading_core import versioning
from trading_core.lab.candles import CANDLE_STALE_CHECK_S
from trading_core.node.bollinger_rsi import STRATEGY_NAME as BOLLINGER_RSI
from trading_core.node.bollinger_rsi import BollingerRsi, BollingerRsiConfig
from trading_core.node.book_imbalance import STRATEGY_NAME as BOOK_IMBALANCE
from trading_core.node.book_imbalance import BookImbalance, BookImbalanceConfig
from trading_core.node.buy_and_hold import STRATEGY_NAME as BUY_AND_HOLD
from trading_core.node.buy_and_hold import BuyAndHold, BuyAndHoldConfig
from trading_core.node.delta_divergence import STRATEGY_NAME as DELTA_DIVERGENCE
from trading_core.node.delta_divergence import DeltaDivergence, DeltaDivergenceConfig
from trading_core.node.macd_baseline import STRATEGY_NAME as MACD_BASELINE
from trading_core.node.macd_baseline import MacdBaseline, MacdBaselineConfig
from trading_core.node.maker_probe import MakerProbe, MakerProbeConfig
from trading_core.node.placeholder import STRATEGY_NAME as PLACEHOLDER
from trading_core.node.placeholder import PlaceholderConfig, PlaceholderTimed, Record
from trading_core.node.random_taker import RandomTaker, RandomTakerConfig
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.node.vwap_retest import STRATEGY_NAME as VWAP_RETEST
from trading_core.node.vwap_retest import VwapRetest, VwapRetestConfig
from trading_core.recording.enums import Env, StrategyKind
from trading_core.recording.rows import JsonObject
from trading_core.recording.spool import WriteItem
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import EntryBlocks
from trading_core.versioning import FrozenHashes

type ConfigValue = int | str | bool
type Factory = Callable[[StrategyConfig, PauseGate, EntryBlocks, Record], KfsStrategy]

_BASE_FIELDS: Final = frozenset(StrategyConfig.__struct_fields__)


class StrategyConfigError(Exception):
    """``[strategy.config]`` does not fit the strategy; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class MarketContextValues:
    """The run file's ``[context]`` values a strategy's market context is built with (spec 0009 AC-13).

    The defaults are ``ContextSettings``'; ``liquidations_available`` is false for ``tardis`` runs.
    """

    max_age_mark_s: int = 30
    max_age_index_s: int = 30
    max_age_open_interest_s: int = 30
    max_age_funding_s: int = 7_200
    liquidation_window_s: int = 60
    liquidations_available: bool = True

    def config_fields(self) -> dict[str, object]:
        """The ``context_*`` fields of ``KfsStrategyConfig``."""
        return {
            "context_max_age_mark_s": self.max_age_mark_s,
            "context_max_age_index_s": self.max_age_index_s,
            "context_max_age_open_interest_s": self.max_age_open_interest_s,
            "context_max_age_funding_s": self.max_age_funding_s,
            "context_liquidation_window_s": self.liquidation_window_s,
            "context_liquidations_available": self.liquidations_available,
        }


@dataclass(frozen=True)
class RunContext:
    """What the run, not ``[strategy.config]``, gives every strategy."""

    env: Env
    run_id: UUID
    instrument_id: str
    seed: int
    risk: RiskLimits
    order_id_tag: str
    taker_fee: Decimal  # the run's fee, which the engine's instrument copy also charges (AC-5)
    market: MarketContextValues = field(default_factory=MarketContextValues)
    # A candle run's stale quote block (spec 0014 AC-13): the run file's limit, checked every 60 s.
    stale_quote_s: Decimal = Decimal(300)
    stale_check_s: int = CANDLE_STALE_CHECK_S
    # Which lab builds the strategy: a scalping candidate takes the candle fields only in a candle run (spec 0017 AC-7).
    lab: Literal["replay", "candle"] = "replay"


# Stands in for the run while checking a config at startup, before any run exists.
_CHECK_CONTEXT: Final = RunContext(
    env=Env.DEMO,
    run_id=UUID(int=0),
    instrument_id="PF_XBTUSD.KRAKEN",
    seed=0,
    risk=RiskLimits(
        max_order_notional=Decimal(1),
        max_leverage=Decimal(1),
        max_total_exposure=Decimal(1),
        risk_per_trade=Decimal("0.005"),
        maintenance_margin_rate=Decimal("0.01"),
        min_liq_distance_pct=Decimal(10),
        liq_stop_multiple=Decimal(3),
        stop_slippage_bps=Decimal(5),
    ),
    order_id_tag="S1",
    taker_fee=Decimal("0.0005"),
)


def _discard(item: WriteItem) -> None:
    """The record callable of a strategy built only to be checked."""


@dataclass(frozen=True)
class StrategyEntry:
    """One registered strategy: its class, its config class, and the run fields it takes."""

    name: str
    config_cls: type[StrategyConfig]
    factory: Factory
    run_fields: Callable[[RunContext], dict[str, object]]
    # The config's ``entry_mode`` default, or None when it has no ``entry_mode`` (``--pair-entry`` needs one, AC-6).
    default_entry_mode: str | None = None
    # ``lab replay`` runs scalping strategies, ``lab backtest`` hour level ones (spec 0014 AC-11).
    kind: StrategyKind = StrategyKind.SCALPING
    # The ``trading_core.node`` module the strategy class lives in, for its frozen hashes (spec 0015
    # AC-1). None for a test probe, whose versions keep NULL hashes and so can never be promoted.
    module: str | None = None
    # A scalping strategy the candle lab may screen (spec 0017 AC-7, AC-8); never held out there.
    candle_screen: bool = False
    # A candle screen strategy whose candle study may be a gate study (spec 0017 AC-20).
    candle_gate: bool = False

    def frozen(self, config: JsonObject) -> FrozenHashes | None:
        """The version's ``code_hash`` and ``frozen_hash`` with ``config``; None for a strategy without a module."""
        return None if self.module is None else versioning.freeze(self.module, config)

    def entry_mode(self, user: Mapping[str, ConfigValue]) -> str | None:
        """The entry mode ``[strategy.config]`` gives, else the default; None for a strategy without one."""
        if self.default_entry_mode is None:
            return None
        return str(user.get("entry_mode", self.default_entry_mode))

    def user_fields(self) -> frozenset[str]:
        """The keys ``[strategy.config]`` may set: the config's own fields, minus the run's."""
        own = frozenset(self.config_cls.__struct_fields__) - _BASE_FIELDS
        return own - frozenset(self.run_fields(_CHECK_CONTEXT))

    def build_config(self, user: Mapping[str, ConfigValue], context: RunContext) -> StrategyConfig:
        """The strategy's config from ``[strategy.config]`` plus the run's values.

        Raises ``StrategyConfigError`` for an unknown or reserved key, or a value of the wrong type.
        """
        allowed = self.user_fields()
        problems = [
            f"{key}: not a setting of {self.name} (allowed: {sorted(allowed)})" for key in user if key not in allowed
        ]
        if problems:
            raise StrategyConfigError(sorted(problems))
        values = {**user, **self.run_fields(context)}
        try:
            config: StrategyConfig = msgspec.convert(values, type=self.config_cls, strict=False)
        except msgspec.ValidationError as exc:
            raise StrategyConfigError([str(exc)]) from None
        return config

    def config_value(self, user: Mapping[str, ConfigValue], name: str) -> object:
        """One field of the config ``[strategy.config]`` builds (its default when not set), checked at startup.

        Raises ``StrategyConfigError`` like ``check_config``.
        """
        return getattr(self.build_config(user, _CHECK_CONTEXT), name, None)

    def check_config(self, user: Mapping[str, ConfigValue]) -> None:
        """Validate ``[strategy.config]`` at startup by building the config for a stand in run."""
        self.build_config(user, _CHECK_CONTEXT)

    def check_strategy(self, user: Mapping[str, ConfigValue]) -> None:
        """Validate ``[strategy.config]`` (or one grid point of it) by building the config and the
        strategy itself for a stand in run, so a value its constructor refuses fails at startup.

        Raises ``StrategyConfigError`` like ``check_config``, and for a ``ValueError`` of the constructor.
        """
        config = self.build_config(user, _CHECK_CONTEXT)
        gate = PauseGate()
        try:
            self.build(config, gate, EntryBlocks(), _discard)
        except ValueError as exc:
            raise StrategyConfigError([str(exc)]) from None

    def build(self, config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
        """The strategy, with the same constructor arguments the trader passes."""
        return self.factory(config, gate, blocks, record)


def _risk_fields(risk: RiskLimits, taker_fee: Decimal) -> dict[str, object]:
    """The ``GuardedConfig`` fields of the run's ``RiskLimits`` and taker fee (spec 0011)."""
    return {
        "taker_fee": str(taker_fee),
        "max_order_notional_usd": str(risk.max_order_notional),
        "max_leverage": str(risk.max_leverage),
        "max_total_exposure_usd": str(risk.max_total_exposure),
        "risk_per_trade": str(risk.risk_per_trade),
        "maintenance_margin_rate": str(risk.maintenance_margin_rate),
        "min_liq_distance_pct": str(risk.min_liq_distance_pct),
        "liq_stop_multiple": str(risk.liq_stop_multiple),
        "stop_slippage_bps": str(risk.stop_slippage_bps),
    }


def _random_taker_fields(context: RunContext) -> dict[str, object]:
    return {
        "env": context.env.value,
        "run_id": str(context.run_id),
        "instrument_id": context.instrument_id,
        "seed": context.seed,
        **_risk_fields(context.risk, context.taker_fee),
        "order_id_tag": context.order_id_tag,
        **context.market.config_fields(),
    }


def _placeholder_fields(context: RunContext) -> dict[str, object]:
    # The trader's placeholder has no seed: its side comes from the bars (spec 0006).
    return {
        "env": context.env.value,
        "run_id": str(context.run_id),
        "instrument_id": context.instrument_id,
        **_risk_fields(context.risk, context.taker_fee),
        "order_id_tag": context.order_id_tag,
        **context.market.config_fields(),
    }


def _candle_fields(context: RunContext) -> dict[str, object]:
    # A candle strategy: the placeholder's run fields plus the candle run's stale quote block (spec 0014).
    return {
        **_placeholder_fields(context),
        "stale_quote_s": str(context.stale_quote_s),
        "stale_check_s": context.stale_check_s,
    }


def _scalp_fields(context: RunContext) -> dict[str, object]:
    # A scalping candidate (spec 0017 AC-7): the placeholder's run fields, plus the stale quote block in a candle run.
    return _candle_fields(context) if context.lab == "candle" else _placeholder_fields(context)


def _placeholder(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, PlaceholderConfig):
        raise TypeError(f"placeholder_timed needs a PlaceholderConfig, not {type(config).__name__}")
    return PlaceholderTimed(config, gate, blocks, record)


def _random_taker(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, RandomTakerConfig):
        raise TypeError(f"random_taker needs a RandomTakerConfig, not {type(config).__name__}")
    return RandomTaker(config, gate, blocks, record)


def _maker_probe(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, MakerProbeConfig):
        raise TypeError(f"maker_probe needs a MakerProbeConfig, not {type(config).__name__}")
    return MakerProbe(config, gate, blocks, record)


def _buy_and_hold(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, BuyAndHoldConfig):
        raise TypeError(f"buy_and_hold needs a BuyAndHoldConfig, not {type(config).__name__}")
    return BuyAndHold(config, gate, blocks, record)


def _macd_baseline(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, MacdBaselineConfig):
        raise TypeError(f"macd_baseline needs a MacdBaselineConfig, not {type(config).__name__}")
    return MacdBaseline(config, gate, blocks, record)


def _bollinger_rsi(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, BollingerRsiConfig):
        raise TypeError(f"bollinger_rsi needs a BollingerRsiConfig, not {type(config).__name__}")
    return BollingerRsi(config, gate, blocks, record)


def _vwap_retest(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, VwapRetestConfig):
        raise TypeError(f"vwap_retest needs a VwapRetestConfig, not {type(config).__name__}")
    return VwapRetest(config, gate, blocks, record)


def _delta_divergence(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, DeltaDivergenceConfig):
        raise TypeError(f"delta_divergence needs a DeltaDivergenceConfig, not {type(config).__name__}")
    return DeltaDivergence(config, gate, blocks, record)


def _book_imbalance(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, BookImbalanceConfig):
        raise TypeError(f"book_imbalance needs a BookImbalanceConfig, not {type(config).__name__}")
    return BookImbalance(config, gate, blocks, record)


def _peek_probe(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, MacdBaselineConfig):
        raise TypeError(f"peek_probe needs a MacdBaselineConfig, not {type(config).__name__}")
    return PeekProbe(config, gate, blocks, record)


def _raise_probe(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, RaiseProbeConfig):
        raise TypeError(f"raise_probe needs a RaiseProbeConfig, not {type(config).__name__}")
    return RaiseProbe(config, gate, blocks, record)


REGISTRY: Final[Mapping[str, StrategyEntry]] = {
    "random_taker": StrategyEntry(
        name="random_taker",
        module=RandomTaker.__module__,
        config_cls=RandomTakerConfig,
        factory=_random_taker,
        run_fields=_random_taker_fields,
    ),
    "maker_probe": StrategyEntry(
        name="maker_probe",
        module=MakerProbe.__module__,
        config_cls=MakerProbeConfig,
        factory=_maker_probe,
        run_fields=_random_taker_fields,
        default_entry_mode="maker",
    ),
    # The paper loop's own strategy, for the parity check (AC-15); its run file uses [paper].
    PLACEHOLDER: StrategyEntry(
        name=PLACEHOLDER,
        module=PlaceholderTimed.__module__,
        config_cls=PlaceholderConfig,
        factory=_placeholder,
        run_fields=_placeholder_fields,
    ),
    # Candle lab strategies (spec 0014): hour level, run by ``lab backtest`` only.
    BUY_AND_HOLD: StrategyEntry(
        name=BUY_AND_HOLD,
        module=BuyAndHold.__module__,
        config_cls=BuyAndHoldConfig,
        factory=_buy_and_hold,
        run_fields=_candle_fields,
        kind=StrategyKind.HOUR_LEVEL,
    ),
    MACD_BASELINE: StrategyEntry(
        name=MACD_BASELINE,
        module=MacdBaseline.__module__,
        config_cls=MacdBaselineConfig,
        factory=_macd_baseline,
        run_fields=_candle_fields,
        kind=StrategyKind.HOUR_LEVEL,
    ),
    # The scalping candidates (spec 0017): replay studies, and a candle screen for the bar ideas.
    BOLLINGER_RSI: StrategyEntry(
        name=BOLLINGER_RSI,
        module=BollingerRsi.__module__,
        config_cls=BollingerRsiConfig,
        factory=_bollinger_rsi,
        run_fields=_scalp_fields,
        default_entry_mode="maker",
        candle_screen=True,
        candle_gate=True,
    ),
    VWAP_RETEST: StrategyEntry(
        name=VWAP_RETEST,
        module=VwapRetest.__module__,
        config_cls=VwapRetestConfig,
        factory=_vwap_retest,
        run_fields=_scalp_fields,
        default_entry_mode="maker",
        candle_screen=True,
    ),
    # Replay only: the candle lab's trades carry no aggressor side.
    DELTA_DIVERGENCE: StrategyEntry(
        name=DELTA_DIVERGENCE,
        module=DeltaDivergence.__module__,
        config_cls=DeltaDivergenceConfig,
        factory=_delta_divergence,
        run_fields=_scalp_fields,
        default_entry_mode="maker",
    ),
    # Replay only: it reads the L2 book, which the candle lab does not have (spec 0017 AC-22).
    BOOK_IMBALANCE: StrategyEntry(
        name=BOOK_IMBALANCE,
        module=BookImbalance.__module__,
        config_cls=BookImbalanceConfig,
        factory=_book_imbalance,
        run_fields=_scalp_fields,
        default_entry_mode="maker",
    ),
}


# Only with LAB_TEST_STRATEGIES=1 (tests): a maker_probe that raises at a set time (AC-13), and a
# macd_baseline that decides about each bar from the next one (spec 0014 AC-19).
TEST_REGISTRY: Final[Mapping[str, StrategyEntry]] = {
    "peek_probe": StrategyEntry(
        name="peek_probe",
        config_cls=MacdBaselineConfig,
        factory=_peek_probe,
        run_fields=_candle_fields,
        kind=StrategyKind.HOUR_LEVEL,
    ),
    "raise_probe": StrategyEntry(
        name="raise_probe",
        config_cls=RaiseProbeConfig,
        factory=_raise_probe,
        run_fields=_random_taker_fields,
        default_entry_mode="maker",
    ),
}


CANDLE_BAR_STEPS: Final = frozenset({BarAggregation.MINUTE, BarAggregation.HOUR, BarAggregation.DAY})


def bar_spec_problem(text: object) -> str | None:
    """Why ``bar_spec`` cannot drive a candle strategy, or None (spec 0014 AC-11).

    Only a time step of ``MINUTE``, ``HOUR``, or ``DAY`` with price type ``LAST`` is accepted.
    """
    try:
        spec = BarSpecification.from_str(str(text))
    except ValueError, KeyError:
        return f"bar_spec {text!r} is not a bar specification like 1-HOUR-LAST"
    if spec.aggregation not in CANDLE_BAR_STEPS or spec.price_type != PriceType.LAST or spec.step < 1:
        return f"bar_spec {text!r}: only MINUTE, HOUR, or DAY steps with LAST prices are accepted"
    return None


def registry_entry(name: str, *, test_strategies: bool = False) -> StrategyEntry:
    """The registered strategy called ``name``; ``StrategyConfigError`` when there is none.

    ``test_strategies`` adds the test only entries (``LAB_TEST_STRATEGIES=1``).
    """
    known = {**REGISTRY, **TEST_REGISTRY} if test_strategies else REGISTRY
    entry = known.get(name)
    if entry is None:
        raise StrategyConfigError([f"strategy.name {name!r} is not registered (known: {sorted(known)})"])
    return entry

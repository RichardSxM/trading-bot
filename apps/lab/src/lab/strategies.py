"""The lab's strategy registry (spec 0008, *Strategy registry*).

Each entry names the paper loop's own strategy class, its config class, and how the run's
values reach that config. A replay builds the strategy exactly as the trader does, with a
pause gate, entry blocks, and a ``record`` callable (AC-7); nothing here subclasses or patches
a strategy.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

import msgspec
from nautilus_trader.config import StrategyConfig

from trading_core.node.placeholder import Record
from trading_core.node.random_taker import RandomTaker, RandomTakerConfig
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import Env
from trading_core.risk.check import RiskLimits
from trading_core.strategy.blocks import EntryBlocks

type ConfigValue = int | str | bool
type Factory = Callable[[StrategyConfig, PauseGate, EntryBlocks, Record], KfsStrategy]

_BASE_FIELDS: Final = frozenset(StrategyConfig.__struct_fields__)


class StrategyConfigError(Exception):
    """``[strategy.config]`` does not fit the strategy; ``problems`` lists every issue."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class RunContext:
    """What the run, not ``[strategy.config]``, gives every strategy."""

    env: Env
    run_id: UUID
    instrument_id: str
    seed: int
    risk: RiskLimits
    order_id_tag: str


# Stands in for the run while checking a config at startup, before any run exists.
_CHECK_CONTEXT: Final = RunContext(
    env=Env.DEMO,
    run_id=UUID(int=0),
    instrument_id="PF_XBTUSD.KRAKEN",
    seed=0,
    risk=RiskLimits(max_order_notional=Decimal(1), max_leverage=Decimal(1)),
    order_id_tag="S1",
)


@dataclass(frozen=True)
class StrategyEntry:
    """One registered strategy: its class, its config class, and the run fields it takes."""

    name: str
    config_cls: type[StrategyConfig]
    factory: Factory
    run_fields: Callable[[RunContext], dict[str, object]]
    # True when the config has an ``entry_mode`` (``--pair-entry`` needs one, AC-6).
    has_entry_mode: bool = False

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

    def check_config(self, user: Mapping[str, ConfigValue]) -> None:
        """Validate ``[strategy.config]`` at startup by building the config for a stand in run."""
        self.build_config(user, _CHECK_CONTEXT)

    def build(self, config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
        """The strategy, with the same constructor arguments the trader passes."""
        return self.factory(config, gate, blocks, record)


def _random_taker_fields(context: RunContext) -> dict[str, object]:
    return {
        "env": context.env.value,
        "run_id": str(context.run_id),
        "instrument_id": context.instrument_id,
        "seed": context.seed,
        "max_order_notional_usd": str(context.risk.max_order_notional),
        "max_leverage": str(context.risk.max_leverage),
        "order_id_tag": context.order_id_tag,
    }


def _random_taker(config: StrategyConfig, gate: PauseGate, blocks: EntryBlocks, record: Record) -> KfsStrategy:
    if not isinstance(config, RandomTakerConfig):
        raise TypeError(f"random_taker needs a RandomTakerConfig, not {type(config).__name__}")
    return RandomTaker(config, gate, blocks, record)


REGISTRY: Final[Mapping[str, StrategyEntry]] = {
    "random_taker": StrategyEntry(
        name="random_taker",
        config_cls=RandomTakerConfig,
        factory=_random_taker,
        run_fields=_random_taker_fields,
    ),
}


def registry_entry(name: str) -> StrategyEntry:
    """The registered strategy called ``name``; ``StrategyConfigError`` when there is none."""
    entry = REGISTRY.get(name)
    if entry is None:
        raise StrategyConfigError([f"strategy.name {name!r} is not registered (known: {sorted(REGISTRY)})"])
    return entry

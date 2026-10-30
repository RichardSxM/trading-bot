"""The demo paper loop's strategy, picked by ``PAPER_STRATEGY``.

``placeholder_timed`` (the default) is the spec 0006 thread: it enters every interval and exits on a timer.
``donchian_4h`` is the candle lab's best rule (spec 0020.3), at the grid point the ``donchian_4h_pair`` study
chose (spec 0022 *Results*). ``donchian_scalp`` is the same rule and point on 5 minute bars, for quicker
trades. ``donchian_scalp_r1`` (spec 0025) adds an EMA 200 trend filter, a midline exit, and a spread check,
and is seeded from Kraken's candles at start (``trader.warmup``). None passed the gate, so on demo each paper
trades only after a manual ``candidate → paper`` promote; live never runs them (the config builders refuse).
They build their own bars from trades, so without a seed they need about 21 bars after every start before
the first entry (3.5 days on 4 hours, under 2 hours on 5 minutes), and r1 about 200 (17 hours).
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Self
from uuid import UUID

from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.orders import Order
from pydantic import model_validator

from trading_core import versioning
from trading_core.node import donchian_4h, donchian_scalp, donchian_scalp_r1, placeholder
from trading_core.node.bar_rule import BarRuleStrategy, Phase
from trading_core.node.donchian_4h import Donchian4h, Donchian4hConfig
from trading_core.node.donchian_scalp import DonchianScalp, DonchianScalpConfig
from trading_core.node.donchian_scalp_r1 import DonchianScalpR1, DonchianScalpR1Config
from trading_core.node.funding import FundingLedger
from trading_core.node.kraken import BTC_PERP
from trading_core.node.paper_snapshots import PaperStrategyView
from trading_core.node.placeholder import PlaceholderConfig, PlaceholderTimed
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.recording.enums import StrategyKind
from trading_core.recording.rows import JsonObject
from trading_core.recording.spool import WriteItem
from trading_core.settings import ContextSettings, PaperLoopSettings
from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.blocks import EntryBlocks
from trading_core.versioning import FrozenHashes

type PaperStrategyName = Literal["placeholder_timed", "donchian_4h", "donchian_scalp", "donchian_scalp_r1"]

# The donchian_4h_pair study's chosen grid point (spec 0022 Results: grid-2), also used by donchian_scalp.
DONCHIAN_ENTRY_BARS: Final = 20
DONCHIAN_STOP_ATR: Final = Decimal(3)
# Its fixed settings, as in apps/lab/studies/donchian_4h.toml.
DONCHIAN_ATR_PERIOD: Final = 14
DONCHIAN_ATR_FLOOR_MULT: Final = Decimal(1)
DONCHIAN_TIME_STOP_BARS: Final = 60
# donchian_scalp_r1's additions (spec 0025): the trend EMA, and the demo's spread limit in ATRs.
R1_EMA_PERIOD: Final = 200
R1_SPREAD_MAX_ATR: Final = Decimal("0.1")
# A PAPER_INSTRUMENTS entry: a Kraken perpetual (spec 0025 AC-15).
_PAPER_INSTRUMENT: Final = re.compile(r"PF_[A-Z0-9]+\.KRAKEN")


class StrategySettings(PaperLoopSettings, ContextSettings):
    """The settings a paper strategy is built from; ``TraderSettings`` adds the rest of the trader's."""

    paper_strategy: PaperStrategyName = "placeholder_timed"
    # The coins it trades, comma separated, any order (spec 0025 AC-15). Only r1 trades more than one.
    paper_instruments: str = BTC_PERP

    @property
    def instrument_list(self) -> list[str]:
        """``PAPER_INSTRUMENTS`` as a sorted list, so the order it is written in changes nothing."""
        return sorted(coin.strip() for coin in self.paper_instruments.split(","))

    @model_validator(mode="after")
    def _instruments_are_valid(self) -> Self:
        coins = self.instrument_list
        for coin in coins:
            if not _PAPER_INSTRUMENT.fullmatch(coin):
                raise ValueError(f"PAPER_INSTRUMENTS entry {coin!r} is not PF_<SYMBOL>.KRAKEN")
        if len(set(coins)) != len(coins):
            raise ValueError("PAPER_INSTRUMENTS holds a duplicate")
        if len(coins) > 1 and self.paper_strategy != donchian_scalp_r1.STRATEGY_NAME:
            raise ValueError(f"more than one PAPER_INSTRUMENTS coin needs {donchian_scalp_r1.STRATEGY_NAME}")
        return self


@dataclass(frozen=True)
class PaperVersion:
    """The strategy version the trader records at start: name, kind, config, and both hashes."""

    name: str
    kind: StrategyKind
    config: JsonObject
    frozen: FrozenHashes


def paper_version(settings: StrategySettings) -> PaperVersion:
    """The version for ``PAPER_STRATEGY``; ``placeholder_timed``'s config is unchanged by this setting."""
    if settings.paper_strategy == donchian_scalp_r1.STRATEGY_NAME:
        r1_config: JsonObject = {
            **_donchian_version_config(settings, donchian_scalp_r1.STRATEGY_NAME, donchian_scalp_r1.BAR_SPEC),
            "ema_period": R1_EMA_PERIOD,
            "spread_max_atr": str(R1_SPREAD_MAX_ATR),
            # The coin set (spec 0025 AC-17): one promote covers it, a different set is a new version.
            "instruments": list(settings.instrument_list),
        }
        frozen = versioning.freeze(DonchianScalpR1.__module__, r1_config)
        return PaperVersion(donchian_scalp_r1.STRATEGY_NAME, StrategyKind.SCALPING, r1_config, frozen)
    if settings.paper_strategy == donchian_scalp.STRATEGY_NAME:
        config = _donchian_version_config(settings, donchian_scalp.STRATEGY_NAME, donchian_scalp.BAR_SPEC)
        frozen = versioning.freeze(DonchianScalp.__module__, config)
        return PaperVersion(donchian_scalp.STRATEGY_NAME, StrategyKind.SCALPING, config, frozen)
    if settings.paper_strategy == donchian_4h.STRATEGY_NAME:
        config = _donchian_version_config(settings, donchian_4h.STRATEGY_NAME, donchian_4h.BAR_SPEC)
        frozen = versioning.freeze(Donchian4h.__module__, config)
        return PaperVersion(donchian_4h.STRATEGY_NAME, StrategyKind.HOUR_LEVEL, config, frozen)
    placeholder_config_json: JsonObject = dict(settings.strategy_config())
    frozen = versioning.freeze(PlaceholderTimed.__module__, placeholder_config_json)
    return PaperVersion(placeholder.STRATEGY_NAME, StrategyKind.SCALPING, placeholder_config_json, frozen)


def _donchian_version_config(settings: StrategySettings, name: str, bar_spec: str) -> JsonObject:
    return {
        "paper_strategy": name,
        "bar_spec": bar_spec,
        "entry_bars": DONCHIAN_ENTRY_BARS,
        "stop_atr": str(DONCHIAN_STOP_ATR),
        "atr_period": DONCHIAN_ATR_PERIOD,
        "atr_floor_mult": str(DONCHIAN_ATR_FLOOR_MULT),
        "time_stop_bars": DONCHIAN_TIME_STOP_BARS,
        **{key: value for key, value in settings.strategy_config().items() if key.startswith("risk_")},
    }


def _demo_only(settings: StrategySettings) -> None:
    # The guard sizes with the sandbox's own taker fee; live has no fee source yet (spec 0011, feature 19).
    if settings.trading_env != "demo":
        raise ValueError("the guard's live taker fee source is not decided yet (spec 0011, feature 19)")


def placeholder_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> PlaceholderConfig:
    """The placeholder's config from the ``PLACEHOLDER_*``, ``RISK_*``, and ``CONTEXT_*`` settings, under ``run_id``.

    The placeholder reads the market context and logs it once per bar (spec 0009 AC-11). The guard
    sizes with the sandbox's own taker fee, the fee each demo fill pays (spec 0011). Raises
    ``ValueError`` outside demo.
    """
    _demo_only(settings)
    return PlaceholderConfig(
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        interval_min=settings.placeholder_interval_min,
        hold_min=settings.placeholder_hold_min,
        lookback_min=settings.placeholder_lookback_min,
        stop_pct=settings.placeholder_stop_pct,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=settings.risk_per_trade,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        stale_quote_s=settings.risk_stale_quote_s,
        stop_accept_timeout_s=settings.stop_accept_timeout_s,
        use_market_context=True,
        context_max_age_mark_s=settings.context_max_age_mark_s,
        context_max_age_index_s=settings.context_max_age_index_s,
        context_max_age_open_interest_s=settings.context_max_age_open_interest_s,
        context_max_age_funding_s=settings.context_max_age_funding_s,
        context_liquidation_window_s=settings.context_liquidation_window_s,
    )


def donchian_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> Donchian4hConfig:
    """``donchian_4h`` at the chosen grid point with the ``RISK_*`` and ``CONTEXT_*`` settings, under ``run_id``.

    Raises ``ValueError`` outside demo.
    """
    _demo_only(settings)
    return Donchian4hConfig(
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=donchian_4h.BAR_SPEC,
        entry_bars=DONCHIAN_ENTRY_BARS,
        stop_atr=DONCHIAN_STOP_ATR,
        atr_period=DONCHIAN_ATR_PERIOD,
        atr_floor_mult=DONCHIAN_ATR_FLOOR_MULT,
        time_stop_bars=DONCHIAN_TIME_STOP_BARS,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=settings.risk_per_trade,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        stale_quote_s=settings.risk_stale_quote_s,
        use_market_context=True,
        context_max_age_mark_s=settings.context_max_age_mark_s,
        context_max_age_index_s=settings.context_max_age_index_s,
        context_max_age_open_interest_s=settings.context_max_age_open_interest_s,
        context_max_age_funding_s=settings.context_max_age_funding_s,
        context_liquidation_window_s=settings.context_liquidation_window_s,
    )


def donchian_scalp_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> DonchianScalpConfig:
    """``donchian_scalp`` (the same point on 5 minute bars) with the ``RISK_*`` and ``CONTEXT_*`` settings.

    Raises ``ValueError`` outside demo.
    """
    _demo_only(settings)
    return DonchianScalpConfig(
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=donchian_scalp.BAR_SPEC,
        entry_bars=DONCHIAN_ENTRY_BARS,
        stop_atr=DONCHIAN_STOP_ATR,
        atr_period=DONCHIAN_ATR_PERIOD,
        atr_floor_mult=DONCHIAN_ATR_FLOOR_MULT,
        time_stop_bars=DONCHIAN_TIME_STOP_BARS,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=settings.risk_per_trade,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        stale_quote_s=settings.risk_stale_quote_s,
        use_market_context=True,
        context_max_age_mark_s=settings.context_max_age_mark_s,
        context_max_age_index_s=settings.context_max_age_index_s,
        context_max_age_open_interest_s=settings.context_max_age_open_interest_s,
        context_max_age_funding_s=settings.context_max_age_funding_s,
        context_liquidation_window_s=settings.context_liquidation_window_s,
    )


def order_id_tag(instrument_id: str) -> str:
    """The symbol without ``PF_``: ``PF_XBTUSD.KRAKEN`` gives ``XBTUSD`` (spec 0025 AC-16)."""
    return instrument_id.removeprefix("PF_").split(".")[0]


def donchian_scalp_r1_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> DonchianScalpR1Config:
    """``donchian_scalp_r1`` (spec 0025) with the EMA 200, the 0.1 ATR spread limit, and the ``RISK_*`` settings.

    Its ``order_id_tag`` is the coin's symbol without ``PF_`` (AC-16), so each coin's strategy has its own id.
    The tag is not in the version config. Raises ``ValueError`` outside demo.
    """
    _demo_only(settings)
    return DonchianScalpR1Config(
        order_id_tag=order_id_tag(instrument_id),
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=donchian_scalp_r1.BAR_SPEC,
        entry_bars=DONCHIAN_ENTRY_BARS,
        stop_atr=DONCHIAN_STOP_ATR,
        atr_period=DONCHIAN_ATR_PERIOD,
        atr_floor_mult=DONCHIAN_ATR_FLOOR_MULT,
        time_stop_bars=DONCHIAN_TIME_STOP_BARS,
        ema_period=R1_EMA_PERIOD,
        spread_max_atr=R1_SPREAD_MAX_ATR,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=settings.risk_per_trade,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        stale_quote_s=settings.risk_stale_quote_s,
        use_market_context=True,
        context_max_age_mark_s=settings.context_max_age_mark_s,
        context_max_age_index_s=settings.context_max_age_index_s,
        context_max_age_open_interest_s=settings.context_max_age_open_interest_s,
        context_max_age_funding_s=settings.context_max_age_funding_s,
        context_liquidation_window_s=settings.context_liquidation_window_s,
    )


class BarRuleView:
    """``PaperStrategyView`` over a ``BarRuleStrategy``, read from its phase and the cache.

    Its time stop counts bar closes and a rule exit can come first, so it reports no exit time.
    """

    def __init__(self, strategy: BarRuleStrategy) -> None:
        self._strategy = strategy

    @property
    def exiting(self) -> bool:
        """True while the exit loop runs."""
        return self._strategy.phase == Phase.EXITING

    @property
    def position_opened_ns(self) -> int | None:
        """When the strategy's open position opened, None while flat."""
        positions = self._strategy.cache.positions_open(strategy_id=self._strategy.id)
        return positions[0].ts_opened if positions else None

    @property
    def exit_due_ns(self) -> int | None:
        """Always None: there is no fixed exit time."""
        return None

    def stop_order(self) -> Order | None:
        """The strategy's open protective stop, if any."""
        for order in self._strategy.cache.orders_open(strategy_id=self._strategy.id):
            if order.order_type == OrderType.STOP_MARKET:
                return order
        return None


def build_paper_strategy(
    settings: StrategySettings,
    run_id: UUID,
    instrument_id: str,
    gate: PauseGate,
    blocks: EntryBlocks,
    record: Callable[[WriteItem], None],
    ledger: FundingLedger,
    seed: Sequence[BarInput] = (),
) -> tuple[KfsStrategy, PaperStrategyView]:
    """The ``PAPER_STRATEGY`` strategy and the view the snapshots read from it; only r1 takes the ``seed``."""
    if settings.paper_strategy == donchian_scalp_r1.STRATEGY_NAME:
        r1 = DonchianScalpR1(donchian_scalp_r1_config(settings, run_id, instrument_id), gate, blocks, record, seed)
        return r1, BarRuleView(r1)
    if settings.paper_strategy == donchian_scalp.STRATEGY_NAME:
        scalp = DonchianScalp(donchian_scalp_config(settings, run_id, instrument_id), gate, blocks, record)
        return scalp, BarRuleView(scalp)
    if settings.paper_strategy == donchian_4h.STRATEGY_NAME:
        donchian = Donchian4h(donchian_config(settings, run_id, instrument_id), gate, blocks, record)
        return donchian, BarRuleView(donchian)
    timed = PlaceholderTimed(placeholder_config(settings, run_id, instrument_id), gate, blocks, record, ledger)
    return timed, timed

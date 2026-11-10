"""The demo paper loop's strategy, picked by ``PAPER_STRATEGY``.

``placeholder_timed`` (the default) is the spec 0006 thread: it enters every interval and exits on a timer.
``donchian_4h`` is the candle lab's best rule (spec 0020.3), at the grid point the ``donchian_4h_pair`` study
chose (spec 0022 *Results*). ``donchian_scalp`` is the same rule and point on 5 minute bars, for quicker
trades. ``donchian_scalp_r1`` (spec 0025) adds an EMA 200 trend filter, a midline exit, and a spread check,
and is seeded from Kraken's candles at start (``trader.warmup``). ``donchian_scalp_r2`` (spec 0026) trades r1's
signals with maker orders and skips entries when ATR is small next to the fee, with the same seed.
``six_vote_trend`` (spec 0029) enters on 1 hour bars when six votes agree, on the same maker base, seeded from
1 hour candles plus BTC's (every coin reads BTC's trend). None passed the gate, so on demo each paper
trades only after a manual ``candidate → paper`` promote; live never runs them (the config builders refuse).
They build their own bars from trades, so without a seed they need about 21 bars after every start before
the first entry (3.5 days on 4 hours, under 2 hours on 5 minutes), and r1 about 200 (17 hours).
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Self
from uuid import UUID

from nautilus_trader.model.enums import OrderType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import Order
from pydantic import model_validator

from trading_core import versioning
from trading_core.node import (
    bar_rule_maker,
    connors_rsi_tp,
    donchian_4h,
    donchian_scalp,
    donchian_scalp_r1,
    donchian_scalp_r2,
    donchian_scalp_tp,
    placeholder,
    six_vote_trend,
    trend_portfolio,
)
from trading_core.node.bar_rule import BarRuleStrategy, Phase
from trading_core.node.bar_rule_maker import MakerBarRuleStrategy
from trading_core.node.connors_rsi_tp import ConnorsRsiTp, ConnorsRsiTpConfig
from trading_core.node.donchian_4h import Donchian4h, Donchian4hConfig
from trading_core.node.donchian_scalp import DonchianScalp, DonchianScalpConfig
from trading_core.node.donchian_scalp_r1 import DonchianScalpR1, DonchianScalpR1Config
from trading_core.node.donchian_scalp_r2 import DonchianScalpR2, DonchianScalpR2Config
from trading_core.node.donchian_scalp_tp import DonchianScalpTp, DonchianScalpTpConfig
from trading_core.node.funding import FundingLedger
from trading_core.node.kraken import BTC_PERP
from trading_core.node.paper_snapshots import PaperStrategyView
from trading_core.node.placeholder import PlaceholderConfig, PlaceholderTimed
from trading_core.node.six_vote_trend import SixVoteTrend, SixVoteTrendConfig
from trading_core.node.strategy import KfsStrategy, PauseGate
from trading_core.node.trend_portfolio import TrendPortfolio, TrendPortfolioConfig
from trading_core.recording.enums import StrategyKind
from trading_core.recording.rows import JsonObject
from trading_core.recording.spool import WriteItem
from trading_core.settings import ContextSettings, PaperLoopSettings
from trading_core.strategy.bar_rule import BarInput
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose
from trading_core.versioning import FrozenHashes

type PaperStrategyName = Literal[
    "placeholder_timed",
    "donchian_4h",
    "donchian_scalp",
    "donchian_scalp_r1",
    "donchian_scalp_r2",
    "six_vote_trend",
    "trend_portfolio",
    "connors_rsi_tp",
    "donchian_scalp_tp",
]

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
# donchian_scalp_r2's additions (spec 0026); it reuses r1's EMA and spread limit.
R2_ENTRY_OFFSET_ATR: Final = Decimal("0.5")
R2_ENTRY_WAIT_BARS: Final = 3
R2_EXIT_WAIT_BARS: Final = 3
R2_ATR_MIN_BPS: Final = Decimal(10)
# donchian_scalp_r2's take profit: a bar close 0.5 % of the entry price in the trade's favour exits.
R2_TAKE_PROFIT_PCT: Final = Decimal("0.5")
# six_vote_trend's settings (spec 0029 AC-2); the rest are its config defaults, recorded in its version.
SIX_TIME_STOP_BARS: Final = 24
SIX_ATR_PERIOD: Final = 14
SIX_ATR_FLOOR_MULT: Final = Decimal(1)
SIX_ENTRY_OFFSET_ATR: Final = Decimal(0)
SIX_ENTRY_WAIT_BARS: Final = 1
SIX_EXIT_WAIT_BARS: Final = 1
SIX_SPREAD_MAX_ATR: Final = Decimal("0.1")
SIX_TRAIL_ATR: Final[Decimal | None] = Decimal(3)
SIX_TRAIL_MIN_STEP_ATR: Final = Decimal("0.1")
SIX_TRAIL_MIN_INTERVAL_S: Final = 5
SIX_TRAIL_MAX_REFUSALS: Final = 5
SIX_BTC_WAIT_S: Final = 3
# Its 1 hour seed (AC-8): keep and minimum bars, outside every version hash.
SIX_SEED_BARS: Final = 300
SIX_SEED_MIN_BARS: Final = 250
# trend_portfolio's settings (spec 0030 AC-3), all recorded in its version.
# connors_rsi_tp's settings: spec 0019.1's 15 minute values, the grid's first point, and the owner's dollar exits.
CONNORS_TIME_STOP_BARS: Final = 12
CONNORS_ATR_PERIOD: Final = 14
CONNORS_ATR_FLOOR_MULT: Final = Decimal(1)
CONNORS_RSI_PERIOD: Final = 2
CONNORS_RSI_LOWER: Final = 5
CONNORS_TREND_SMA: Final = 200
CONNORS_EXIT_SMA: Final = 5
CONNORS_STOP_ATR_MULT: Final = Decimal(3)
CONNORS_TAKE_PROFIT_USD: Final = Decimal(3)
CONNORS_STOP_LOSS_USD: Final = Decimal(2)
# Far above any stop's cost, so the order cap sets every trade's size (see connors_rsi_tp_config).
CONNORS_RISK_PER_TRADE: Final = Decimal("0.5")
CONNORS_SEED_BARS: Final = 300
CONNORS_SEED_MIN_BARS: Final = 250
# donchian_scalp_tp: the donchian_scalp point, the owner's dollar exits, and the order cap as the trade size.
DONCHIAN_TP_TAKE_PROFIT_USD: Final = Decimal(3)
DONCHIAN_TP_STOP_LOSS_USD: Final = Decimal(2)
DONCHIAN_TP_RISK_PER_TRADE: Final = Decimal("0.5")
TP_LOOKBACK_DAYS: Final = 30
TP_VOL_DAYS: Final = 30
# The owner's 5x demo choice (2026-11-09, down from 10x to cut fees), not the tested 0.02 and 2.
TP_VOL_TARGET: Final = Decimal("0.155")
TP_GROSS_CAP: Final = Decimal(5)
TP_MIN_HISTORY_DAYS: Final = 120
TP_REBALANCE_WEEKDAY: Final = 0
TP_RUN_AT_UTC: Final = "00:05"
TP_RETRY_MAX: Final = 3
TP_RETRY_WAIT_S: Final = 60
TP_QUOTE_WAIT_S: Final = 60
TP_START_COOLDOWN_S: Final = 3600
TP_SEED_DAYS: Final = 150
TP_SEED_MIN_DAYS: Final = 121
# The owner's daily take profit (2026-11-10), with the daily stop the replay paired it with: the whole book
# closes for the day once the day's P&L is this share of the book's planned size, either way.
TP_BOOK_TAKE_PROFIT_PCT: Final = Decimal("0.03")
TP_BOOK_STOP_LOSS_PCT: Final = Decimal("0.03")
# The strategies that trade more than one coin (spec 0025 AC-15, spec 0026 AC-16, spec 0029 AC-10).
_MULTI_COIN: Final = (
    donchian_scalp_r1.STRATEGY_NAME,
    donchian_scalp_r2.STRATEGY_NAME,
    six_vote_trend.STRATEGY_NAME,
    trend_portfolio.STRATEGY_NAME,
    connors_rsi_tp.STRATEGY_NAME,
    donchian_scalp_tp.STRATEGY_NAME,
)
# The strategies the trader fetches a warmup seed for before the node (spec 0025 AC-7, spec 0026 AC-16, spec 0029).
SEEDED_STRATEGIES: Final = (
    donchian_scalp_r1.STRATEGY_NAME,
    donchian_scalp_r2.STRATEGY_NAME,
    six_vote_trend.STRATEGY_NAME,
    trend_portfolio.STRATEGY_NAME,
    connors_rsi_tp.STRATEGY_NAME,
    donchian_scalp_tp.STRATEGY_NAME,
)
# A PAPER_INSTRUMENTS entry: a Kraken perpetual (spec 0025 AC-15).
_PAPER_INSTRUMENT: Final = re.compile(r"PF_[A-Z0-9]+\.KRAKEN")


class StrategySettings(PaperLoopSettings, ContextSettings):
    """The settings a paper strategy is built from; ``TraderSettings`` adds the rest of the trader's."""

    paper_strategy: PaperStrategyName = "placeholder_timed"
    # The coins it trades, comma separated, any order (spec 0025 AC-15). Only r1 and r2 trade more than one.
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
        if len(coins) > 1 and self.paper_strategy not in _MULTI_COIN:
            raise ValueError(f"more than one PAPER_INSTRUMENTS coin needs {' or '.join(_MULTI_COIN)}")
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
    if settings.paper_strategy == trend_portfolio.STRATEGY_NAME:
        tp_config: JsonObject = {
            "paper_strategy": trend_portfolio.STRATEGY_NAME,
            "bar_spec": trend_portfolio.BAR_SPEC,
            **_trend_portfolio_settings(),
            **{key: value for key, value in settings.strategy_config().items() if key.startswith("risk_")},
            "instruments": list(settings.instrument_list),
        }
        frozen = versioning.freeze(TrendPortfolio.__module__, tp_config)
        return PaperVersion(trend_portfolio.STRATEGY_NAME, StrategyKind.TREND, tp_config, frozen)
    if settings.paper_strategy == connors_rsi_tp.STRATEGY_NAME:
        connors_config: JsonObject = {
            "paper_strategy": connors_rsi_tp.STRATEGY_NAME,
            "bar_spec": connors_rsi_tp.BAR_SPEC,
            "time_stop_bars": CONNORS_TIME_STOP_BARS,
            "atr_period": CONNORS_ATR_PERIOD,
            "atr_floor_mult": str(CONNORS_ATR_FLOOR_MULT),
            "rsi_period": CONNORS_RSI_PERIOD,
            "rsi_lower": CONNORS_RSI_LOWER,
            "trend_sma": CONNORS_TREND_SMA,
            "exit_sma": CONNORS_EXIT_SMA,
            "stop_atr_mult": str(CONNORS_STOP_ATR_MULT),
            "take_profit_usd": str(CONNORS_TAKE_PROFIT_USD),
            "stop_loss_usd": str(CONNORS_STOP_LOSS_USD),
            **{key: value for key, value in settings.strategy_config().items() if key.startswith("risk_")},
            "risk_per_trade": str(CONNORS_RISK_PER_TRADE),
            "instruments": list(settings.instrument_list),
        }
        frozen = versioning.freeze(ConnorsRsiTp.__module__, connors_config)
        return PaperVersion(connors_rsi_tp.STRATEGY_NAME, StrategyKind.HOUR_LEVEL, connors_config, frozen)
    if settings.paper_strategy == six_vote_trend.STRATEGY_NAME:
        six_config: JsonObject = {
            "paper_strategy": six_vote_trend.STRATEGY_NAME,
            "bar_spec": six_vote_trend.BAR_SPEC,
            "time_stop_bars": SIX_TIME_STOP_BARS,
            "atr_period": SIX_ATR_PERIOD,
            "atr_floor_mult": str(SIX_ATR_FLOOR_MULT),
            "entry_offset_atr": str(SIX_ENTRY_OFFSET_ATR),
            "entry_wait_bars": SIX_ENTRY_WAIT_BARS,
            "exit_wait_bars": SIX_EXIT_WAIT_BARS,
            "atr_min_bps": None,
            "spread_max_atr": str(SIX_SPREAD_MAX_ATR),
            "trail_atr": None if SIX_TRAIL_ATR is None else str(SIX_TRAIL_ATR),
            "trail_min_step_atr": str(SIX_TRAIL_MIN_STEP_ATR),
            "trail_min_interval_s": SIX_TRAIL_MIN_INTERVAL_S,
            "trail_max_refusals": SIX_TRAIL_MAX_REFUSALS,
            "btc_instrument_id": six_vote_trend.BTC_INSTRUMENT_ID,
            "btc_wait_s": SIX_BTC_WAIT_S,
            **{key: value for key, value in settings.strategy_config().items() if key.startswith("risk_")},
            "instruments": list(settings.instrument_list),
        }
        frozen = versioning.freeze(SixVoteTrend.__module__, six_config)
        return PaperVersion(six_vote_trend.STRATEGY_NAME, StrategyKind.HOUR_LEVEL, six_config, frozen)
    if settings.paper_strategy == donchian_scalp_r2.STRATEGY_NAME:
        r2_config: JsonObject = {
            **_donchian_version_config(settings, donchian_scalp_r2.STRATEGY_NAME, donchian_scalp_r2.BAR_SPEC),
            "ema_period": R1_EMA_PERIOD,
            "entry_offset_atr": str(R2_ENTRY_OFFSET_ATR),
            "entry_wait_bars": R2_ENTRY_WAIT_BARS,
            "exit_wait_bars": R2_EXIT_WAIT_BARS,
            "atr_min_bps": str(R2_ATR_MIN_BPS),
            "spread_max_atr": str(R1_SPREAD_MAX_ATR),
            "take_profit_pct": str(R2_TAKE_PROFIT_PCT),
            "instruments": list(settings.instrument_list),
        }
        frozen = versioning.freeze(DonchianScalpR2.__module__, r2_config)
        return PaperVersion(donchian_scalp_r2.STRATEGY_NAME, StrategyKind.SCALPING, r2_config, frozen)
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
    if settings.paper_strategy == donchian_scalp_tp.STRATEGY_NAME:
        donchian_tp_config: JsonObject = {
            **_donchian_version_config(settings, donchian_scalp_tp.STRATEGY_NAME, donchian_scalp_tp.BAR_SPEC),
            "take_profit_usd": str(DONCHIAN_TP_TAKE_PROFIT_USD),
            "stop_loss_usd": str(DONCHIAN_TP_STOP_LOSS_USD),
            "risk_per_trade": str(DONCHIAN_TP_RISK_PER_TRADE),
            "instruments": list(settings.instrument_list),
        }
        frozen = versioning.freeze(DonchianScalpTp.__module__, donchian_tp_config)
        return PaperVersion(donchian_scalp_tp.STRATEGY_NAME, StrategyKind.SCALPING, donchian_tp_config, frozen)
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


def _trend_portfolio_settings() -> JsonObject:
    # Every AC-3 setting, as the version records it (spec 0030 AC-10).
    return {
        "order_id_tag": trend_portfolio.ORDER_ID_TAG,
        "instrument_id": BTC_PERP,
        "lookback_days": TP_LOOKBACK_DAYS,
        "vol_days": TP_VOL_DAYS,
        "vol_target": str(TP_VOL_TARGET),
        "gross_cap": str(TP_GROSS_CAP),
        "min_history_days": TP_MIN_HISTORY_DAYS,
        "rebalance_weekday": TP_REBALANCE_WEEKDAY,
        "run_at_utc": TP_RUN_AT_UTC,
        "retry_max": TP_RETRY_MAX,
        "retry_wait_s": TP_RETRY_WAIT_S,
        "quote_wait_s": TP_QUOTE_WAIT_S,
        "start_cooldown_s": TP_START_COOLDOWN_S,
        "seed_days": TP_SEED_DAYS,
        "seed_min_days": TP_SEED_MIN_DAYS,
        "book_take_profit_pct": str(TP_BOOK_TAKE_PROFIT_PCT),
        "book_stop_loss_pct": str(TP_BOOK_STOP_LOSS_PCT),
    }


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


def donchian_scalp_r2_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> DonchianScalpR2Config:
    """``donchian_scalp_r2`` (spec 0026): r1's settings plus the maker entry offset, both waits, and the ATR floor.

    Its ``order_id_tag`` is the coin's symbol without ``PF_``, as r1's. Raises ``ValueError`` outside demo.
    """
    _demo_only(settings)
    return DonchianScalpR2Config(
        order_id_tag=order_id_tag(instrument_id),
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=donchian_scalp_r2.BAR_SPEC,
        entry_bars=DONCHIAN_ENTRY_BARS,
        stop_atr=DONCHIAN_STOP_ATR,
        atr_period=DONCHIAN_ATR_PERIOD,
        atr_floor_mult=DONCHIAN_ATR_FLOOR_MULT,
        time_stop_bars=DONCHIAN_TIME_STOP_BARS,
        ema_period=R1_EMA_PERIOD,
        entry_offset_atr=R2_ENTRY_OFFSET_ATR,
        entry_wait_bars=R2_ENTRY_WAIT_BARS,
        exit_wait_bars=R2_EXIT_WAIT_BARS,
        atr_min_bps=R2_ATR_MIN_BPS,
        spread_max_atr=R1_SPREAD_MAX_ATR,
        take_profit_pct=R2_TAKE_PROFIT_PCT,
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


def six_vote_trend_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> SixVoteTrendConfig:
    """``six_vote_trend`` (spec 0029 AC-2) with the ``RISK_*`` and ``CONTEXT_*`` settings, under ``run_id``.

    Its ``order_id_tag`` is the coin's symbol without ``PF_``, as r2's. Raises ``ValueError`` outside demo, and
    when ``PAPER_INSTRUMENTS`` does not hold BTC (every coin reads BTC's trend, so BTC's trades must be fed).
    """
    _demo_only(settings)
    if six_vote_trend.BTC_INSTRUMENT_ID not in settings.instrument_list:
        raise ValueError(
            f"six_vote_trend needs {six_vote_trend.BTC_INSTRUMENT_ID} in PAPER_INSTRUMENTS "
            "(every coin reads BTC's trend)"
        )
    return SixVoteTrendConfig(
        order_id_tag=order_id_tag(instrument_id),
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=six_vote_trend.BAR_SPEC,
        time_stop_bars=SIX_TIME_STOP_BARS,
        atr_period=SIX_ATR_PERIOD,
        atr_floor_mult=SIX_ATR_FLOOR_MULT,
        entry_offset_atr=SIX_ENTRY_OFFSET_ATR,
        entry_wait_bars=SIX_ENTRY_WAIT_BARS,
        exit_wait_bars=SIX_EXIT_WAIT_BARS,
        atr_min_bps=None,
        spread_max_atr=SIX_SPREAD_MAX_ATR,
        trail_atr=SIX_TRAIL_ATR,
        trail_min_step_atr=SIX_TRAIL_MIN_STEP_ATR,
        trail_min_interval_s=SIX_TRAIL_MIN_INTERVAL_S,
        trail_max_refusals=SIX_TRAIL_MAX_REFUSALS,
        btc_instrument_id=six_vote_trend.BTC_INSTRUMENT_ID,
        btc_wait_s=SIX_BTC_WAIT_S,
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


def connors_rsi_tp_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> ConnorsRsiTpConfig:
    """``connors_rsi_tp`` with the ``RISK_*`` caps, under ``run_id``. Raises ``ValueError`` outside demo.

    ``CONNORS_RISK_PER_TRADE`` is set far above any stop's cost on purpose, so ``RISK_MAX_ORDER_NOTIONAL_USD``
    sets every trade's size (the owner's fixed size on the demo) instead of the stop distance.
    """
    _demo_only(settings)
    return ConnorsRsiTpConfig(
        order_id_tag=order_id_tag(instrument_id),
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=connors_rsi_tp.BAR_SPEC,
        time_stop_bars=CONNORS_TIME_STOP_BARS,
        atr_period=CONNORS_ATR_PERIOD,
        atr_floor_mult=CONNORS_ATR_FLOOR_MULT,
        rsi_period=CONNORS_RSI_PERIOD,
        rsi_lower=CONNORS_RSI_LOWER,
        trend_sma=CONNORS_TREND_SMA,
        exit_sma=CONNORS_EXIT_SMA,
        stop_atr_mult=CONNORS_STOP_ATR_MULT,
        take_profit_usd=CONNORS_TAKE_PROFIT_USD,
        stop_loss_usd=CONNORS_STOP_LOSS_USD,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=CONNORS_RISK_PER_TRADE,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        stale_quote_s=settings.risk_stale_quote_s,
        use_market_context=True,
    )


def donchian_scalp_tp_config(settings: StrategySettings, run_id: UUID, instrument_id: str) -> DonchianScalpTpConfig:
    """``donchian_scalp_tp`` with the ``RISK_*`` caps, under ``run_id``. Raises ``ValueError`` outside demo.

    ``DONCHIAN_TP_RISK_PER_TRADE`` is far above any stop's cost on purpose, so ``RISK_MAX_ORDER_NOTIONAL_USD``
    sets every trade's size (the owner's fixed size on the demo) instead of the stop distance.
    """
    _demo_only(settings)
    return DonchianScalpTpConfig(
        order_id_tag=order_id_tag(instrument_id),
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=instrument_id,
        bar_spec=donchian_scalp_tp.BAR_SPEC,
        entry_bars=DONCHIAN_ENTRY_BARS,
        stop_atr=DONCHIAN_STOP_ATR,
        atr_period=DONCHIAN_ATR_PERIOD,
        atr_floor_mult=DONCHIAN_ATR_FLOOR_MULT,
        time_stop_bars=DONCHIAN_TIME_STOP_BARS,
        take_profit_usd=DONCHIAN_TP_TAKE_PROFIT_USD,
        stop_loss_usd=DONCHIAN_TP_STOP_LOSS_USD,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=DONCHIAN_TP_RISK_PER_TRADE,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        stale_quote_s=settings.risk_stale_quote_s,
        use_market_context=True,
    )


def trend_portfolio_config(settings: StrategySettings, run_id: UUID) -> TrendPortfolioConfig:
    """``trend_portfolio`` (spec 0030 AC-3) over every ``PAPER_INSTRUMENTS`` coin, with the ``RISK_*`` settings.

    One instance trades every coin under ``order_id_tag`` ``TP``; ``instrument_id`` is BTC, the base's context
    instrument only. Raises ``ValueError`` outside demo, and when ``PAPER_INSTRUMENTS`` does not hold BTC.
    """
    _demo_only(settings)
    return TrendPortfolioConfig(
        order_id_tag=trend_portfolio.ORDER_ID_TAG,
        env=settings.trading_env,
        run_id=str(run_id),
        instrument_id=BTC_PERP,
        instruments=tuple(settings.instrument_list),
        lookback_days=TP_LOOKBACK_DAYS,
        vol_days=TP_VOL_DAYS,
        vol_target=TP_VOL_TARGET,
        gross_cap=TP_GROSS_CAP,
        min_history_days=TP_MIN_HISTORY_DAYS,
        rebalance_weekday=TP_REBALANCE_WEEKDAY,
        run_at_utc=TP_RUN_AT_UTC,
        retry_max=TP_RETRY_MAX,
        retry_wait_s=TP_RETRY_WAIT_S,
        quote_wait_s=TP_QUOTE_WAIT_S,
        start_cooldown_s=TP_START_COOLDOWN_S,
        seed_days=TP_SEED_DAYS,
        seed_min_days=TP_SEED_MIN_DAYS,
        book_take_profit_pct=TP_BOOK_TAKE_PROFIT_PCT,
        book_stop_loss_pct=TP_BOOK_STOP_LOSS_PCT,
        stale_quote_s=settings.risk_stale_quote_s,
        max_order_notional_usd=settings.risk_max_order_notional_usd,
        max_leverage=settings.risk_max_leverage,
        max_total_exposure_usd=settings.risk_max_total_exposure_usd,
        risk_per_trade=settings.risk_per_trade,
        maintenance_margin_rate=settings.risk_maintenance_margin_rate,
        min_liq_distance_pct=settings.risk_min_liq_distance_pct,
        liq_stop_multiple=settings.risk_liq_stop_multiple,
        stop_slippage_bps=settings.risk_stop_slippage_bps,
        taker_fee=settings.sandbox_taker_fee,
        use_market_context=False,
    )


class TrendPortfolioView:
    """``PaperStrategyView`` for one coin of a ``TrendPortfolio`` (spec 0030 AC-10): no stop, no exit time.

    ``exiting`` is always False (rebalances are single market orders); ``position_opened_ns`` is the coin's
    open position's, ``exit_due_ns`` and ``stop_order`` are None.
    """

    def __init__(self, strategy: TrendPortfolio, instrument_id: str) -> None:
        self._strategy = strategy
        self._instrument_id = InstrumentId.from_str(instrument_id)

    @property
    def exiting(self) -> bool:
        """Always False."""
        return False

    @property
    def position_opened_ns(self) -> int | None:
        """When this coin's open position opened, None while flat."""
        positions = self._strategy.cache.positions_open(
            instrument_id=self._instrument_id, strategy_id=self._strategy.id
        )
        return positions[0].ts_opened if positions else None

    @property
    def exit_due_ns(self) -> int | None:
        """Always None: there is no fixed exit time."""
        return None

    def stop_order(self) -> Order | None:
        """Always None: the portfolio places no stop."""
        return None


def build_trend_portfolio(
    settings: StrategySettings,
    run_id: UUID,
    gate: PauseGate,
    blocks: EntryBlocks,
    record: Callable[[WriteItem], None],
    seeds: Mapping[str, Sequence[DailyClose]],
    last_start_ns: int | None,
    last_book_exit_ns: int | None = None,
) -> tuple[TrendPortfolio, list[TrendPortfolioView]]:
    """The single ``trend_portfolio`` instance and one view per coin, in ``PAPER_INSTRUMENTS`` order (AC-10)."""
    strategy = TrendPortfolio(
        trend_portfolio_config(settings, run_id), gate, blocks, record, seeds, last_start_ns, last_book_exit_ns
    )
    return strategy, [TrendPortfolioView(strategy, coin) for coin in settings.instrument_list]


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


class MakerBarRuleView:
    """``PaperStrategyView`` over a ``MakerBarRuleStrategy`` (spec 0026 AC-16): like ``BarRuleView``.

    ``exiting`` is true while an exit limit or a market exit is working. The card shows positions only,
    so a resting entry is not reported.
    """

    def __init__(self, strategy: MakerBarRuleStrategy) -> None:
        self._strategy = strategy

    @property
    def exiting(self) -> bool:
        """True while an exit limit or a market exit is working."""
        return self._strategy.phase == bar_rule_maker.Phase.EXITING

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
    btc_seed: Sequence[BarInput] = (),
) -> tuple[KfsStrategy, PaperStrategyView]:
    """The ``PAPER_STRATEGY`` strategy and the view the snapshots read from it.

    Only r1, r2, and ``six_vote_trend`` take the ``seed``; only ``six_vote_trend`` takes ``btc_seed`` (spec 0029 AC-8).
    """
    if settings.paper_strategy == connors_rsi_tp.STRATEGY_NAME:
        connors = ConnorsRsiTp(connors_rsi_tp_config(settings, run_id, instrument_id), gate, blocks, record, seed)
        return connors, BarRuleView(connors)
    if settings.paper_strategy == donchian_scalp_tp.STRATEGY_NAME:
        scalp_tp = DonchianScalpTp(
            donchian_scalp_tp_config(settings, run_id, instrument_id), gate, blocks, record, seed
        )
        return scalp_tp, BarRuleView(scalp_tp)
    if settings.paper_strategy == six_vote_trend.STRATEGY_NAME:
        six = SixVoteTrend(six_vote_trend_config(settings, run_id, instrument_id), gate, blocks, record, seed, btc_seed)
        return six, MakerBarRuleView(six)
    if settings.paper_strategy == donchian_scalp_r2.STRATEGY_NAME:
        r2 = DonchianScalpR2(donchian_scalp_r2_config(settings, run_id, instrument_id), gate, blocks, record, seed)
        return r2, MakerBarRuleView(r2)
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

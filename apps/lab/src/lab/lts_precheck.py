"""``lab lts-precheck`` (spec 0031 AC-12, AC-15, AC-17 to AC-19): one fixed scan of recorded days.

Each clean segment goes through ``SegmentStream`` once. The runner builds its own ``OrderBook`` from
the deltas it consumes, because the stream's own book runs ahead. Days run in spawned workers, and
nothing depends on the order they finish in.

The ``all_trades`` variant always runs. On our own data ``liquidations_removed`` runs beside it,
feeding each ``LiquidationTick`` to the detector so its trade leaves the flow.
"""

import json
import logging
import multiprocessing
from collections import Counter
from collections.abc import Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from nautilus_trader.core.data import Data
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import CustomData, OrderBookDeltas, QuoteTick, TradeTick
from nautilus_trader.model.enums import BookType
from nautilus_trader.model.identifiers import InstrumentId

from lab.catalog import SourceTree
from lab.instruments import pinned_instrument
from lab.lts_runfile import LoadedLtsRun, LtsRunFile
from lab.replay import plan_days
from lab.settings import LabSettings
from lab.stream import SegmentStream
from trading_core.lab.lts_verdict import DayMarkouts, LtsVerdict, evaluate_lts
from trading_core.lab.markouts import MarkoutTracker
from trading_core.lab.segments import Segment
from trading_core.logs import configure_logging
from trading_core.marketdata.context import LiquidationKind
from trading_core.marketdata.hours import DAY_NS, HOUR_NS
from trading_core.marketdata.normalized import TradeDeduper, TradeKind
from trading_core.node.context_types import LiquidationTick
from trading_core.node.lts_feed import normalized_trade
from trading_core.strategy.lts_features import DepthTrack
from trading_core.strategy.lts_sweep import CandidateView, LtsParams, LtsSide, LtsSignal, SweepDetector

log = logging.getLogger(__name__)

LTS_PRECHECK_VERSION: Final = "1"
PROCESS: Final = "lab"
ALL_TRADES: Final = "all_trades"
LIQUIDATIONS_REMOVED: Final = "liquidations_removed"
HORIZONS_S: Final = (5, 15, 30)
VERDICT_HORIZON_S: Final = 15
WARMUP_NS: Final = HOUR_NS
_BPS: Final = Decimal(10_000)
_FORCED_KINDS: Final = {
    LiquidationKind.LIQUIDATION: TradeKind.LIQUIDATION,
    LiquidationKind.TERMINATION: TradeKind.TERMINATION,
    LiquidationKind.BLOCK: TradeKind.BLOCK,
}


def _book_depth(book: OrderBook, view: CandidateView, band_bps: int, contract_size: Decimal) -> Decimal:
    """Displayed base size on the relevant side within the band around the frozen level (AC-12).

    Sizes come from each level's orders, because ``BookLevel.size()`` is a float.
    """
    band = view.level * band_bps / _BPS
    low, high = view.level - band, view.level + band
    levels = book.bids() if view.side is LtsSide.LONG else book.asks()
    depth = Decimal(0)
    for level in levels:
        price = level.price.as_decimal()
        if low <= price <= high:
            depth += sum((order.size.as_decimal() for order in level.orders()), Decimal(0))
    return depth * contract_size


@dataclass(frozen=True)
class SignalRow:
    """One signal with its depth fields and markouts."""

    signal: LtsSignal
    depth_before: Decimal
    depth_min: Decimal
    depth_after: Decimal
    replenishment_ratio: Decimal
    recovery_ns: int | None
    markouts: Mapping[int, Decimal | None] = field(default_factory=dict)


@dataclass(frozen=True)
class VariantScan:
    """One variant's result over one stretch of events."""

    signals: tuple[SignalRow, ...]
    ended: Counter[str]


@dataclass(frozen=True)
class SegmentScan:
    """One segment's result per variant, with the facts the report needs."""

    variants: Mapping[str, VariantScan]
    first_mid: Decimal | None
    duplicates: int
    liquidations: int
    matched_liquidations: int


class _Variant:
    """One detector with its markouts and the depth of its open candidate."""

    def __init__(self, params: LtsParams, band_bps: int, contract_size: Decimal, *, remove_forced: bool) -> None:
        self.detector = SweepDetector(params)
        self.remove_forced = remove_forced
        self._tracker = MarkoutTracker(HORIZONS_S)
        self._band_bps = band_bps
        self._contract_size = contract_size
        self._view: CandidateView | None = None
        self._track: DepthTrack | None = None
        self._pending: list[tuple[LtsSignal, DepthTrack]] = []

    def on_book(self, book: OrderBook, ts_ns: int) -> None:
        """The book changed: read the open candidate's depth."""
        if self._track is not None and self._view is not None:
            self._track.update(_book_depth(book, self._view, self._band_bps, self._contract_size), ts_ns)

    def on_mid(self, ts_ns: int, mid: Decimal, book: OrderBook) -> None:
        """The mid changed: update markouts and the detector, and keep any signal."""
        self._tracker.on_mid(ts_ns, mid)
        signal = self.detector.on_mid(ts_ns, mid)
        if signal is not None:
            if self._track is None:
                raise ValueError(f"signal at {ts_ns} has no depth track")
            self._tracker.on_fill(
                str(len(self._pending)), buy=signal.side is LtsSide.LONG, fill_px=signal.mid, fill_ns=ts_ns
            )
            self._pending.append((signal, self._track))
        self._sync(book, ts_ns)

    def sync(self, book: OrderBook, ts_ns: int) -> None:
        """Start tracking depth for a new candidate, or stop for an ended one."""
        self._sync(book, ts_ns)

    def _sync(self, book: OrderBook, ts_ns: int) -> None:
        view = self.detector.candidate
        if view is None:
            self._view, self._track = None, None
        elif view is not self._view:
            self._view = view
            self._track = DepthTrack(_book_depth(book, view, self._band_bps, self._contract_size), ts_ns)

    def finish(self, last_book_ns: int) -> VariantScan:
        """Close the segment and pair each signal with its markouts."""
        self.detector.finish()
        markouts = {done.trade_id: done.markouts for done in self._tracker.finish(last_book_ns)}
        rows = tuple(
            SignalRow(
                signal=signal,
                depth_before=track.before,
                depth_min=track.minimum,
                depth_after=track.after,
                replenishment_ratio=track.ratio,
                recovery_ns=track.recovery_ns,
                markouts=markouts[str(index)],
            )
            for index, (signal, track) in enumerate(self._pending)
        )
        return VariantScan(rows, Counter({reason.value: n for reason, n in self.detector.ended.items()}))


def scan_events(
    events: Iterable[Data],
    *,
    params: LtsParams,
    instrument_id: str,
    contract_size: Decimal,
    band_bps: int,
    with_forced_variant: bool,
) -> SegmentScan:
    """Run one segment's events through every variant, in stream order."""
    book = OrderBook(InstrumentId.from_str(instrument_id), BookType.L2_MBP)
    variants = {ALL_TRADES: _Variant(params, band_bps, contract_size, remove_forced=False)}
    if with_forced_variant:
        variants[LIQUIDATIONS_REMOVED] = _Variant(params, band_bps, contract_size, remove_forced=True)
    deduper = TradeDeduper()
    last_top: tuple[int, int] | None = None
    first_mid: Decimal | None = None
    last_book_ns = 0
    duplicates = 0
    trade_ids: set[str] = set()
    liquidation_ids: set[str] = set()
    for event in events:
        if isinstance(event, OrderBookDeltas):
            book.apply_deltas(event)
            last_book_ns = event.ts_init
            for variant in variants.values():
                variant.on_book(book, event.ts_init)
        elif isinstance(event, QuoteTick):
            top = (event.bid_price.raw, event.ask_price.raw)
            if top == last_top:
                continue
            last_top = top
            mid: Decimal = (event.bid_price.as_decimal() + event.ask_price.as_decimal()) / 2
            if first_mid is None:
                first_mid = mid
            for variant in variants.values():
                variant.on_mid(event.ts_init, mid, book)
        elif isinstance(event, TradeTick):
            trade = normalized_trade(event, contract_size)
            if not deduper.first_seen(trade):
                duplicates += 1
                continue
            if with_forced_variant:
                trade_ids.add(trade.trade_id)
            for variant in variants.values():
                variant.detector.on_trade(trade)
                variant.sync(book, event.ts_init)
        elif isinstance(event, CustomData) and isinstance(event.data, LiquidationTick):
            tick = event.data
            liquidation_ids.add(tick.trade_id)
            for variant in variants.values():
                if variant.remove_forced:
                    variant.detector.on_forced(
                        tick.trade_id,
                        tick.aggressor_side,
                        tick.price.as_decimal() * tick.size.as_decimal() * contract_size,
                        tick.ts_init,
                        _FORCED_KINDS[tick.kind],
                    )
                    variant.sync(book, tick.ts_init)
    return SegmentScan(
        variants={name: variant.finish(last_book_ns) for name, variant in variants.items()},
        first_mid=first_mid,
        duplicates=duplicates,
        liquidations=len(liquidation_ids),
        matched_liquidations=len(liquidation_ids & trade_ids),
    )


def params_of(run: LtsRunFile, tick: Decimal) -> LtsParams:
    """The detector's settings from the run file and the instrument's tick."""
    return LtsParams(
        tick=tick,
        lookback_s=run.lookback_s,
        sweep_buffer_ticks=run.sweep_buffer_ticks,
        reclaim_buffer_ticks=run.reclaim_buffer_ticks,
        invalidation_buffer_ticks=run.invalidation_buffer_ticks,
        reclaim_timeout_ms=run.reclaim_timeout_ms,
        efficiency_pct_min=run.efficiency_pct_min,
        horizon_s=run.horizon_s,
    )


def scan_segment(
    tree: SourceTree,
    segment: Segment,
    *,
    params: LtsParams,
    contract_size: Decimal,
    band_bps: int,
    with_forced_variant: bool,
    cut_ns: int | None = None,
) -> SegmentScan:
    """One segment scanned from disk. ``cut_ns`` ends the stream after that time (the no look ahead check)."""

    def events() -> Iterable[Data]:
        for chunk in SegmentStream(tree, segment).chunks():
            for event in chunk:
                if cut_ns is not None and event.ts_init > cut_ns:
                    return
                yield event

    return scan_events(
        events(),
        params=params,
        instrument_id=tree.instrument_id,
        contract_size=contract_size,
        band_bps=band_bps,
        with_forced_variant=with_forced_variant,
    )


@dataclass(frozen=True)
class DayScan:
    """One day: its segments' results and how much time was eligible."""

    day: date
    segments: int
    clean_hours: int
    skipped: Mapping[str, int]
    eligible_ns: int
    tick_bps: Decimal | None
    scans: tuple[SegmentScan, ...]


def scan_day(
    root: Path, day: date, params: LtsParams, contract_size: Decimal, band_bps: int, instrument: str
) -> DayScan:
    """Every clean segment of one day, with a 60 minute warm up taken off each."""
    tree = SourceTree(root, instrument)
    plan = plan_days(tree, [day])
    with_forced = root.name == "own"
    scans: list[SegmentScan] = []
    eligible = 0
    for segment in plan.segments:
        eligible += max(0, segment.end_ns - segment.start_ns + 1 - WARMUP_NS)
        scans.append(
            scan_segment(
                tree,
                segment,
                params=params,
                contract_size=contract_size,
                band_bps=band_bps,
                with_forced_variant=with_forced,
            )
        )
    first_mid = next((s.first_mid for s in scans if s.first_mid is not None), None)
    log.info("lts_precheck_day", extra={"fields": {"day": day.isoformat(), "segments": len(plan.segments)}})
    return DayScan(
        day=day,
        segments=len(plan.segments),
        clean_hours=sum(len(s.hours) for s in plan.segments),
        skipped=dict(plan.skipped_by_reason()),
        eligible_ns=eligible,
        tick_bps=params.tick / first_mid * _BPS if first_mid is not None else None,
        scans=tuple(scans),
    )


def _worker(
    root: Path, day: date, params: LtsParams, contract_size: Decimal, band_bps: int, instrument: str
) -> DayScan:
    configure_logging(process=PROCESS, env="demo")
    return scan_day(root, day, params, contract_size, band_bps, instrument)


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _signal_json(row: SignalRow, day: date, segment: int, variant: str) -> dict[str, object]:
    signal = row.signal
    return {
        "day": day.isoformat(),
        "segment": segment,
        "variant": variant,
        "side": signal.side.value,
        "ts_init": signal.ts_init_ns,
        "started_ns": signal.started_ns,
        "latched_ns": signal.latched_ns,
        "mid": str(signal.mid),
        "level": str(signal.level),
        "extreme": str(signal.extreme),
        "buy_notional_1s": str(signal.buy_notional_1s),
        "sell_notional_1s": str(signal.sell_notional_1s),
        "impact_bps": str(signal.impact_bps),
        "efficiency": str(signal.efficiency),
        "efficiency_rank": str(signal.efficiency_rank),
        "depth_before": str(row.depth_before),
        "depth_min": str(row.depth_min),
        "depth_after": str(row.depth_after),
        "replenishment_ratio": str(row.replenishment_ratio),
        "recovery_ns": row.recovery_ns,
        "markouts_bps": {str(h): _text(row.markouts[h]) for h in HORIZONS_S},
    }


def _variant_rows(day: DayScan, variant: str) -> list[SignalRow]:
    return [row for scan in day.scans for row in scan.variants[variant].signals]


def _day_markouts(day: DayScan, variant: str) -> DayMarkouts:
    values = (row.markouts[VERDICT_HORIZON_S] for row in _variant_rows(day, variant))
    return DayMarkouts(day.day.isoformat(), tuple(m for m in values if m is not None), day.eligible_ns)


def _variant_day_json(day: DayScan, variant: str) -> dict[str, object]:
    rows = _variant_rows(day, variant)
    ended: Counter[str] = Counter()
    for scan in day.scans:
        ended.update(scan.variants[variant].ended)
    markouts = _day_markouts(day, variant).markouts
    return {
        "signals": len(rows),
        "signals_with_15s": len(markouts),
        "mean_15s_bps": _text(sum(markouts, Decimal(0)) / len(markouts) if markouts else None),
        "past_segment_end": {str(h): sum(1 for r in rows if r.markouts[h] is None) for h in HORIZONS_S},
        "ended": dict(sorted(ended.items())),
    }


def _day_json(day: DayScan, variants: tuple[str, ...]) -> dict[str, object]:
    return {
        "day": day.day.isoformat(),
        "segments": day.segments,
        "clean_hours": day.clean_hours,
        "skipped_hours": dict(sorted(day.skipped.items())),
        "eligible_ns": day.eligible_ns,
        "tick_bps": _text(day.tick_bps),
        "duplicates": sum(s.duplicates for s in day.scans),
        "variants": {variant: _variant_day_json(day, variant) for variant in variants},
    }


def _verdict_json(verdict: LtsVerdict) -> dict[str, object]:
    return {
        "verdict": verdict.verdict,
        "cost_bps": str(verdict.cost_bps),
        "label": verdict.label,
        "trials": 1,
        "checks": [
            {"name": c.name, "value": _text(c.value), "threshold": str(c.threshold), "passed": c.passed}
            for c in verdict.checks
        ],
    }


def _cross_check_json(days: list[DayScan]) -> dict[str, object]:
    def block(variant: str) -> dict[str, object]:
        markouts = [m for day in days for m in _day_markouts(day, variant).markouts]
        signals = sum(len(_variant_rows(day, variant)) for day in days)
        return {
            "signals": signals,
            "signals_with_15s": len(markouts),
            "mean_15s_bps": _text(sum(markouts, Decimal(0)) / len(markouts) if markouts else None),
        }

    matched = sum(s.matched_liquidations for day in days for s in day.scans)
    return {
        ALL_TRADES: block(ALL_TRADES),
        LIQUIDATIONS_REMOVED: block(LIQUIDATIONS_REMOVED),
        "liquidations": sum(s.liquidations for day in days for s in day.scans),
        "matched_trade_ids": matched,
        "label": "no_op" if matched == 0 else "matched",
    }


def build_report(
    loaded: LoadedLtsRun,
    source: str,
    git_commit: str,
    results: list[DayScan],
    *,
    started_at: str,
    finished_at: str,
) -> dict[str, object]:
    """The JSON report. Only the two time fields differ between runs on the same inputs."""
    run = loaded.run
    own = source == "own"
    variants = (ALL_TRADES, LIQUIDATIONS_REMOVED) if own else (ALL_TRADES,)
    days = sorted(results, key=lambda d: d.day)
    verdict = evaluate_lts(
        [_day_markouts(day, ALL_TRADES) for day in days],
        maker_fee=run.maker_fee,
        taker_fee=run.taker_fee,
        hurdle_k=run.hurdle_k,
        min_signals_per_day=run.min_signals_per_day,
        min_positive_days=run.min_positive_days,
    )
    return {
        "lts_precheck_version": LTS_PRECHECK_VERSION,
        "git_commit": git_commit,
        "run_file_hash": loaded.file_hash,
        "source": source,
        "instrument_id": run.instrument_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "days": [_day_json(day, variants) for day in days],
        "signals": [
            _signal_json(row, day.day, index, variant)
            for day in days
            for variant in variants
            for index, scan in enumerate(day.scans)
            for row in scan.variants[variant].signals
        ],
        "total_eligible_days": str(Decimal(sum(day.eligible_ns for day in days)) / DAY_NS),
        "verdict": None if own else _verdict_json(verdict),
        "cross_check": _cross_check_json(days) if own else None,
    }


def report_path(reports_dir: Path, source: str, file_hash: str, git_commit: str) -> Path:
    """Where this run's report goes."""
    return reports_dir / "lts-precheck" / f"{source}_{file_hash[:12]}_{git_commit[:12]}.json"


def missing_days_message(source: str, missing: list[date]) -> str:
    """The exit 2 message: the fix depends on the source."""
    days = ", ".join(day.isoformat() for day in missing)
    if source == "tardis":
        return f"lab lts-precheck: no manifest for these Tardis days: {days}; fetch them with `lab import-tardis`"
    return f"lab lts-precheck: no manifest for these own catalog days: {days}; copy them from the VPS, read only"


def run_precheck(
    settings: LabSettings, loaded: LoadedLtsRun, source: str, days: list[date], now_start: datetime
) -> int:
    """Scan ``days`` and write the report (0, a pass or a fail), or exit 2 for a day with no manifest."""
    run = loaded.run
    root = settings.lab_catalog_dir / source
    tree = SourceTree(root, run.instrument_id)
    missing = tree.missing_manifests(days)
    if missing:
        print(missing_days_message(source, missing))
        return 2
    instrument = pinned_instrument(run.instrument_id)
    params = params_of(run, instrument.price_increment.as_decimal())
    contract_size = instrument.multiplier.as_decimal()
    log.info("lts_precheck_start", extra={"fields": {"source": source, "days": len(days)}})
    count = len(days)
    with ProcessPoolExecutor(max_workers=settings.lab_workers, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(
            pool.map(
                _worker,
                [root] * count,
                days,
                [params] * count,
                [contract_size] * count,
                [run.depth_band_bps] * count,
                [run.instrument_id] * count,
            )
        )
    report = build_report(
        loaded,
        source,
        settings.git_commit,
        results,
        started_at=now_start.isoformat(),
        finished_at=datetime.now(UTC).isoformat(),
    )
    path = report_path(settings.lab_reports_dir, source, loaded.file_hash, settings.git_commit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(path)
    log.info("lts_precheck_done", extra={"fields": {"path": str(path)}})
    return 0

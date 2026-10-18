"""``lab fetch-candles``: Kraken's 1 minute trade candles and funding into the candles tree (spec 0014 AC-1, AC-2).

Funding is fetched once and merged into every month its rows are charged in, then each candle
month is paged, checked, and written: bars, gaps, and last the manifest, the month's commit point.
A complete month is skipped unless ``--refetch`` is given. A month that fails writes nothing, and
the months before it stay.
"""

import logging
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from lab.instruments import pinned_instrument
from lab.kraken_http import FetchFailedError, KrakenHttp
from lab.settings import LabSettings
from lab.tardis_import import tree_bytes
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candles import (
    MINUTE_NS,
    FundingRow,
    Month,
    MonthManifest,
    RawCandle,
    build_candles,
    check_funding_stamp,
    find_gaps,
    is_complete,
    merge_funding,
    quiet_prefix_problems,
    rows_by_month,
)
from trading_core.lab.errors import BadRowsError
from trading_core.lab.tardis_days import fits_under_cap
from trading_core.marketdata.hours import GB, SECOND_NS

log = logging.getLogger(__name__)

INSTRUMENT_ID: Final = "PF_XBTUSD.KRAKEN"
SYMBOL: Final = "PF_XBTUSD"
CANDLES_PATH: Final = f"/api/charts/v1/trade/{SYMBOL}/1m"
FUNDING_PATH: Final = "/derivatives/api/v4/historicalfundingrates"
# The expected size of one stored month, for the catalog cap check (AC-5).
CANDLE_MONTH_GB: Final = Decimal("0.1")

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_CATALOG_FULL: Final = 3


def candle_tree(catalog_dir: Path) -> CandleTree:
    """The candles tree under the lab catalog, for the pinned instrument."""
    return CandleTree(catalog_dir / "candles", pinned_instrument(INSTRUMENT_ID))


def _iso(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / SECOND_NS, UTC).isoformat().replace("+00:00", "Z")


def log_bad_rows(exc: BadRowsError) -> None:
    """ERROR ``lab_candles_bad_rows`` with every bad row as (time, field, value)."""
    rows = [{"ts": ts, "field": name, "value": value} for ts, name, value in exc.rows]
    log.error("lab_candles_bad_rows", extra={"fields": {"scope": exc.scope, "rows": rows}})


# Funding ---------------------------------------------------------------------------------------


def api_funding_rows(body: Any, url: str) -> list[FundingRow]:
    """The endpoint's rows as funding rows, every one checked (AC-2, AC-3)."""
    try:
        rates = body["rates"]
        parsed = [
            (
                int(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()),
                Decimal(r["relativeFundingRate"]),
            )
            for r in rates
        ]
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise FetchFailedError(url, 200, f"unexpected funding body: {exc!r}") from None
    bad = [b for stamp, rate in parsed for b in check_funding_stamp(stamp * SECOND_NS, rate)]
    if bad:
        raise BadRowsError("funding api", bad)
    return [FundingRow.from_stamp(stamp * SECOND_NS, rate, "api") for stamp, rate in parsed]


def merge_into_tree(tree: CandleTree, rows: Iterable[FundingRow], scope: str) -> tuple[int, int, int]:
    """Merge funding rows into their charge time months: every month is checked before the first
    write, then each changed month's file is written and an existing manifest's funding fields are
    recomputed. Returns (rows added, rows already present, months touched). Raises ``BadRowsError``."""
    merges = {month: merge_funding(tree.read_funding(month), new) for month, new in rows_by_month(rows).items()}
    conflicts = [c for merge in merges.values() for c in merge.conflicts]
    if conflicts:
        raise BadRowsError(scope, conflicts)
    touched = 0
    for month, merge in sorted(merges.items()):
        if not merge.added:
            continue
        tree.write_funding(month, merge.rows)
        manifest = tree.read_manifest(month)
        if manifest is not None:
            tree.write_manifest(manifest.with_funding(merge.rows))
        touched += 1
    return sum(m.added for m in merges.values()), sum(m.present for m in merges.values()), touched


# Candles ---------------------------------------------------------------------------------------


def page_month(http: KrakenHttp, month: Month, end_ns: int) -> list[RawCandle]:
    """Every candle Kraken returns for ``month`` up to ``end_ns``, paging by ``more_candles`` and the last time."""
    start_s, end_s = month.start_ns // SECOND_NS, end_ns // SECOND_NS
    url = http.base_url + CANDLES_PATH
    candles: list[RawCandle] = []
    since = start_s
    while since < end_s:
        body = http.get_json(CANDLES_PATH, {"from": since, "to": end_s})
        try:
            page = [
                RawCandle(
                    int(c["time"]), str(c["open"]), str(c["high"]), str(c["low"]), str(c["close"]), str(c["volume"])
                )
                for c in body["candles"]
            ]
            more = bool(body.get("more_candles"))
        except (KeyError, TypeError, ValueError) as exc:
            raise FetchFailedError(url, 200, f"unexpected candles body: {exc!r}") from None
        candles.extend(page)
        if not page or not more:
            break
        next_since = page[-1].time_ms // 1000 + 60
        if next_since <= since:
            raise FetchFailedError(url, 200, "paging did not advance")
        since = next_since
    return candles


def last_close(tree: CandleTree, month: Month) -> Decimal | None:
    """The close of ``month``'s last stored candle, or ``None`` when the month is not committed or holds none."""
    if tree.read_manifest(month) is None:
        return None
    rows = tree.read_bars(month)
    return rows[-1].close if rows else None


def fetch_month(tree: CandleTree, http: KrakenHttp, month: Month, fetch_start_ns: int) -> MonthManifest:
    """Fetch, check, and commit one month. Raises ``BadRowsError`` or ``FetchFailedError`` before any write.

    A quiet first row is checked against the previous month's last stored close (AC-3). An old
    manifest is removed before the first write, so a month killed partway has none and is fetched
    again from scratch.
    """
    instrument = tree.instrument
    complete = is_complete(month, fetch_start_ns)
    end_ns = month.end_ns if complete else fetch_start_ns - fetch_start_ns % MINUTE_NS
    raw = page_month(http, month, end_ns)
    build = build_candles(
        raw,
        month,
        instrument.price_increment.as_decimal(),
        instrument.size_increment.as_decimal(),
        prev_close=last_close(tree, month.previous()),
    )
    if build.bad:
        raise BadRowsError(str(month), build.bad)
    if build.quiet_unchecked:
        log.warning("lab_candles_quiet_unchecked", extra={"fields": {"month": str(month)}})
    rows = [r for r in build.rows if r.open_ns < end_ns]
    missing, gaps = find_gaps(rows, month.start_ns, end_ns)
    tree.manifest_file(month).unlink(missing_ok=True)
    tree.write_bars(month, rows)
    tree.write_gaps(month, gaps)
    manifest = MonthManifest(
        month=str(month),
        bar_rows=len(rows),
        quiet_minutes=sum(r.quiet for r in rows),
        volume_rounded_rows=build.volume_rounded_rows,
        zero_volume_trade_rows=build.zero_volume_trade_rows,
        funding_rows=0,
        funding_sources=[],
        first_ts_ns=rows[0].ts_ns if rows else None,
        last_ts_ns=rows[-1].ts_ns if rows else None,
        missing_minutes=missing,
        gap_count=len(gaps),
        missing_funding_charges=0,
        complete=complete,
        fetched_at=_iso(fetch_start_ns),
    ).with_funding(tree.read_funding(month))
    tree.write_manifest(manifest)
    return manifest


def check_next_month(tree: CandleTree, month: Month) -> None:
    """After a ``--refetch`` of ``month``: the next month's stored leading quiet rows must be flat at
    ``month``'s last close (AC-3). Raises ``BadRowsError`` scoped to the next month."""
    following, close = month.next(), last_close(tree, month)
    if close is None or tree.read_manifest(following) is None:
        return
    bad = quiet_prefix_problems(tree.read_bars(following), close)
    if bad:
        raise BadRowsError(str(following), bad)


def fetch_candles(
    settings: LabSettings,
    months: list[Month],
    *,
    refetch: bool = False,
    http: KrakenHttp | None = None,
    clock: Callable[[], int] = time.time_ns,
) -> int:
    """Fetch funding once, then each month in order. Returns the exit code (AC-1, AC-2, AC-5).

    A complete month is skipped unless ``refetch`` is set.
    """
    tree = candle_tree(settings.lab_catalog_dir)
    client = http or KrakenHttp(settings.lab_kraken_futures_url, settings.lab_kraken_pause_ms)
    fetch_start_ns = clock()
    try:
        url = client.base_url + FUNDING_PATH
        funding = api_funding_rows(client.get_json(FUNDING_PATH, {"symbol": SYMBOL}), url)
        added, present, touched = merge_into_tree(tree, funding, "funding api")
        log.info(
            "lab_funding_fetched",
            extra={"fields": {"rows": len(funding), "added": added, "present": present, "months_touched": touched}},
        )
        for month in months:
            manifest = tree.read_manifest(month)
            if manifest is not None and manifest.complete and not refetch:
                log.info("lab_candles_month_skipped", extra={"fields": {"month": str(month)}})
                continue
            used = tree_bytes(settings.lab_catalog_dir)
            if not fits_under_cap(used, CANDLE_MONTH_GB, settings.lab_catalog_cap_gb):
                log.error(
                    "lab_catalog_full",
                    extra={
                        "fields": {
                            "month": str(month),
                            "used_gb": round(used / GB, 3),
                            "month_gb": str(CANDLE_MONTH_GB),
                            "cap_gb": str(settings.lab_catalog_cap_gb),
                        }
                    },
                )
                return EXIT_CATALOG_FULL
            done = fetch_month(tree, client, month, fetch_start_ns)
            log.info(
                "lab_candles_month_done",
                extra={
                    "fields": {
                        "month": done.month,
                        "bar_rows": done.bar_rows,
                        "quiet_minutes": done.quiet_minutes,
                        "volume_rounded_rows": done.volume_rounded_rows,
                        "missing_minutes": done.missing_minutes,
                        "gap_count": done.gap_count,
                        "funding_rows": done.funding_rows,
                        "missing_funding_charges": done.missing_funding_charges,
                        "complete": done.complete,
                    }
                },
            )
            if refetch:
                check_next_month(tree, month)
    except BadRowsError as exc:
        log_bad_rows(exc)
        return EXIT_FAILED
    except FetchFailedError as exc:
        log.error(
            "lab_fetch_failed",
            extra={"fields": {"url": exc.url, "status": exc.status, "reason": exc.reason}},
        )
        return EXIT_FAILED
    return EXIT_OK

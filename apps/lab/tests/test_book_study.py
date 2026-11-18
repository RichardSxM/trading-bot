"""covers: spec 0033 AC-19 (``lab study`` runs a book strategy as one book unit over every coin, a coin that lists
inside the range included, and its fills match the book mode's before the range end) and AC-19c (a book run
and a book study's benchmarks pay no funding where no rate is stored, and count it)."""

import random
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from lab.book_mode import BOOK_RISK, BookModePlan, daily_seed, midnight_ns, run_book_mode
from lab.book_unit import run_book_unit, seed_closes
from lab.candle_backtest import book_range_problems
from lab.candle_fetch import candle_tree
from lab.candle_units import CandleUnitPlan
from lab.runfile import Overrides, RunFileError, candle_run_from_raw
from lab.strategies import REGISTRY, TREND_VOL_SETTINGS, candle_entry, is_book_strategy
from lab.study import _benchmark_run
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month, MonthManifest
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import PositionStatus, StrategyKind

D = Decimal
BTC, ETH, SOL = "PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN"
HISTORY_FROM = date(2026, 1, 1)
FIRST = date(2026, 5, 10)  # 129 daily closes before it
LAST = date(2026, 5, 13)
LISTED = date(2026, 5, 11)  # SOL's first minute: inside the range, so it streams but never has the history
EQUITY = D(10000)
COST = D("0.0007")


def _walk(seed: int, start_ns: int, minutes: int, first: int, drift: int) -> list[CandleRow]:
    """1 minute candles on whole dollars with a drift, a quiet stretch at 00:00 to 00:04 UTC every day."""
    rng = random.Random(seed)
    rows: list[CandleRow] = []
    close = D(first)
    for m in range(minutes):
        open_ns = start_ns + m * MINUTE_NS
        if (open_ns // MINUTE_NS) % 1440 < 5:
            rows.append(CandleRow(open_ns + MINUTE_NS, close, close, close, close, D(0), True))
            continue
        o = close
        close = max(D(100), o + rng.randint(-20 + drift, 20 + drift))
        rows.append(CandleRow(open_ns + MINUTE_NS, o, max(o, close) + 1, min(o, close) - 1, close, D(2), False))
    return rows


def _write(catalog: Path, coin: str, seed: int, first: int, drift: int, start: date, funded: bool) -> None:
    tree = candle_tree(catalog, coin)
    start_ns = midnight_ns(start)
    end_ns = midnight_ns(LAST + timedelta(days=1))
    by_month: dict[Month, list[CandleRow]] = {}
    for row in _walk(seed, start_ns, (end_ns - start_ns) // MINUTE_NS, first, drift):
        by_month.setdefault(Month.of_ns(row.open_ns), []).append(row)
    for month, rows in by_month.items():
        tree.write_bars(month, rows)
        charges = [t for t in range(month.start_ns, month.next().start_ns, HOUR_NS) if t >= midnight_ns(FIRST)]
        if funded and charges:
            tree.write_funding(month, [FundingRow(t, D("0.0005"), HOUR_NS, "api") for t in charges])
        manifest = MonthManifest(
            str(month),
            len(rows),
            0,
            0,
            0,
            0,
            0,
            [],
            rows[0].ts_ns,
            rows[-1].ts_ns,
            0,
            0,
            0,
            False,
            "2026-10-01T00:00:00Z",
        )
        tree.write_manifest(manifest.with_funding(tree.read_funding(month)))


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    root = tmp_path_factory.mktemp("book_study")
    _write(root, BTC, 1, 60000, 1, HISTORY_FROM, funded=True)
    _write(root, ETH, 2, 3000, -1, HISTORY_FROM, funded=False)  # no stored rate: pays nothing, counted
    _write(root, SOL, 3, 150, 1, LISTED, funded=False)
    return root


def _unit_plan(catalog: Path) -> CandleUnitPlan:
    return CandleUnitPlan(
        variant="base",
        candles_root=catalog / "candles",
        instrument_ids=(BTC, ETH, SOL),
        start_ns=midnight_ns(FIRST),
        end_ns=midnight_ns(LAST) + 12 * HOUR_NS,  # past the last 00:05 rebalance, as a study's 24:00 end is
        maker_fee=COST,
        taker_fee=COST,
        slippage_bps=D(0),
        starting_balance_usd=EQUITY,
        leverage=D(10),
        risk=BOOK_RISK,
        seed=0,
        strategy_name="trend_vol",
        strategy_config={"rebalance_band": "0.3"},
        stale_quote_s=D(300),
    )


@pytest.fixture(scope="module")
def unit(catalog):
    return run_book_unit(_unit_plan(catalog))


@pytest.fixture(scope="module")
def mode(catalog):
    trees = {coin: candle_tree(catalog, coin) for coin in (BTC, ETH, SOL)}
    return run_book_mode(
        BookModePlan(
            strategy="trend_vol",
            candles_root=catalog / "candles",
            coins=(BTC, ETH, SOL),
            first=FIRST,
            last=LAST,
            equity=EQUITY,
            cost=COST,
            config={"rebalance_band": "0.3"},
            seeds={coin: seed_closes(tree, FIRST, 150) for coin, tree in trees.items()},
            funding={BTC: {t: D("0.0005") for t in range(midnight_ns(FIRST), midnight_ns(LAST) + HOUR_NS, HOUR_NS)}},
        )
    )


def test_a_book_strategy_is_found_for_candle_runs_only():
    assert is_book_strategy("trend_vol")
    assert "trend_vol" not in REGISTRY  # the replay lab and the per coin path still refuse it
    entry = candle_entry("trend_vol")
    assert (entry.name, entry.kind) == ("trend_vol", StrategyKind.TREND)
    assert not is_book_strategy("tsmom")
    assert candle_entry("tsmom").name == "tsmom"


def test_the_seed_is_the_daily_seed_of_the_stored_hour_closes(catalog):
    tree = candle_tree(catalog, BTC)
    hourly = {c.ts_ns: c.close for m in (Month(2026, 1), Month(2026, 5)) for c in tree.read_bars(m)}
    seed = seed_closes(tree, FIRST, 150)

    assert len(seed) == 129  # every close from 2026-01-01 to 05-09
    assert seed[-5:] == daily_seed(hourly, FIRST, 150)[-5:]
    assert seed_closes(candle_tree(catalog, SOL), FIRST, 150) == ()


def test_the_book_unit_fills_as_the_book_mode_does_before_the_range_end(unit, mode):
    def key(f):
        return (f.instrument_id, f.side, f.quantity, f.price, f.filled_ns)

    assert mode.fill_rows
    # The unit fills exactly as the book mode (fills sharing a nanosecond may come in another coin order), and
    # then its range end close flattens every coin the book mode leaves open.
    extra = Counter(key(f) for _, f in unit.fills) - Counter(key(f) for f in mode.fill_rows)
    assert Counter(key(f) for f in mode.fill_rows) - Counter(key(f) for _, f in unit.fills) == Counter()
    held = {c for c, q in mode.days[-1].positions.items() if q != 0}
    assert held
    assert sorted(coin for coin, *_ in extra.elements()) == sorted(held)
    assert all(p.status == PositionStatus.CLOSED for _, p in unit.positions)


def test_a_coin_listed_inside_the_range_streams_and_never_trades_without_its_history(unit):
    assert set(unit.per_instrument) == {BTC, ETH, SOL}
    assert unit.per_instrument[SOL].last_point_ns > midnight_ns(LISTED)
    assert not any(f.instrument_id == SOL for _, f in unit.fills)


def test_every_coin_is_sampled_at_every_charge_time(unit):
    assert set(unit.funding_samples) == {BTC, ETH, SOL}
    counts = {len(s) for s in unit.funding_samples.values()}
    assert len(counts) == 1
    assert counts.pop() > 0


def test_a_late_coin_is_checked_from_its_first_stored_day(catalog):
    start, end = midnight_ns(FIRST), midnight_ns(LAST + timedelta(days=1)) - MINUTE_NS
    assert book_range_problems(candle_tree(catalog, SOL), start, end) == []
    assert book_range_problems(candle_tree(catalog, ETH), start, end) == []  # no funding check at all
    early = midnight_ns(date(2025, 12, 1))
    # A coin whose first committed month comes after the range start is a late listing, checked from its first day.
    assert book_range_problems(candle_tree(catalog, BTC), early, end) == []
    assert "no committed candles" in book_range_problems(candle_tree(catalog, SOL), early, early + HOUR_NS)[0]


def test_the_empty_months_committed_before_a_listing_are_skipped(tmp_path):
    tree = candle_tree(tmp_path, SOL)
    for month in (Month(2026, 3), Month(2026, 4)):  # committed with no minute, as the fetcher does before a listing
        tree.write_bars(month, [])
        tree.write_manifest(MonthManifest(str(month), 0, 0, 0, 0, 0, 0, [], None, None, 0, 0, 0, False, "x"))
    _write(tmp_path, SOL, 3, 150, 1, LISTED, funded=False)
    start, end = midnight_ns(date(2026, 3, 1)), midnight_ns(LAST + timedelta(days=1)) - MINUTE_NS

    assert book_range_problems(tree, start, end) == []


RUN = {
    "instrument_ids": [BTC, ETH, SOL],
    "from": date(2026, 5, 10),
    "to": date(2026, 5, 12),
    "seed": 7,
    "stale_quote_s": 300,
    "maker_fee": "0.0002",
    "taker_fee": "0.0005",
    "slippage_bps": "2",
    "starting_balance_usd": "10000",
    "leverage": "5",
    "risk": {"max_order_notional": "5000", "max_leverage": "9", "max_total_exposure": "50000"},
    "strategy": {
        "name": "trend_vol",
        "config": {k: v for k, v in TREND_VOL_SETTINGS.items() if k != "lookback_days"},
        "grid": {"lookback_days": [20, 30, 40, 60]},
    },
}


def test_a_book_run_file_loads_over_several_coins_with_its_grid():
    loaded = candle_run_from_raw("study.toml", b"", RUN, Overrides(), allow_multi=True)

    assert loaded.run.instruments == (ETH, SOL, BTC)  # sorted by id
    assert loaded.bar_spec == "1-DAY-LAST"
    assert loaded.run.version_config({"lookback_days": 30})["instrument_ids"] == [ETH, SOL, BTC]


def test_a_book_run_file_on_one_coin_is_refused():
    one = {k: v for k, v in RUN.items() if k != "instrument_ids"} | {"instrument_id": BTC}
    with pytest.raises(RunFileError, match="is a book strategy"):
        candle_run_from_raw("study.toml", b"", one, Overrides(), allow_multi=True)


BUY_AND_HOLD = str(Path(__file__).parents[1] / "runs" / "buy_and_hold.toml")


def test_a_book_studys_benchmark_buys_without_the_market_context_and_splits_the_notional():
    market = ((BTC, ETH, SOL), D("0.0002"), D("0.0005"))
    book = _benchmark_run(BUY_AND_HOLD, market, date(2026, 5, 10), date(2026, 5, 12), book=True)
    plain = _benchmark_run(BUY_AND_HOLD, market, date(2026, 5, 10), date(2026, 5, 12))

    assert book.run.strategy.config["use_market_context"] is False
    assert "use_market_context" not in plain.run.strategy.config
    assert book.run.strategy.config["notional_usd"] == plain.run.strategy.config["notional_usd"]
    assert D(str(plain.run.strategy.config["notional_usd"])) == D(10000) / 3

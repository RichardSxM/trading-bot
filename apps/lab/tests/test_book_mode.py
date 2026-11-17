"""covers: spec 0033 AC-10 (the lab's book mode: one ``trend_vol`` instance over every coin, seeds from the candles
tree, ``from`` the first traded day, the trader's start rebalance) and AC-10a (the independent copy imports
nothing from ``trading_core`` and matches the pure replay; the Nautilus book mode matches it within the pass
rule), on a synthetic two coin tree."""

import ast
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import lab.book
from lab.book import TREND_VOL, independent_days, load_coin
from lab.book_mode import BOOK_RISK, BookModePlan, daily_seed, midnight_ns, run_book_mode
from lab.candle_fetch import candle_tree
from lab.strategies import BOOK_REGISTRY, REGISTRY, RunContext, book_entry
from trading_core.lab.book_parity import BookDay, compare_books
from trading_core.lab.book_replay import ReplayConfig, replay_book, trend_vol_planner
from trading_core.lab.candles import MINUTE_NS, CandleRow, FundingRow, Month
from trading_core.marketdata.hours import HOUR_NS
from trading_core.node.strategy import PauseGate
from trading_core.node.trend_vol import TrendVol, TrendVolConfig
from trading_core.recording.enums import Env
from trading_core.strategy.blocks import EntryBlocks
from trading_core.strategy.trend_portfolio import DailyClose, PortfolioParams
from trading_core.strategy.trend_vol import BookVolParams

D = Decimal
BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
HISTORY_FROM = date(2026, 1, 1)
FIRST = date(2026, 5, 10)  # 129 daily closes before it
LAST = date(2026, 5, 13)
EQUITY = D(10000)
COST = D("0.0007")


def _walk(seed: int, start_ns: int, minutes: int, first: int, drift: int) -> list[CandleRow]:
    """1 minute candles on whole dollars with a drift, a quiet minute every 701, and a quiet stretch at 00:00
    to 00:04 UTC on every day (so the book's first quote comes only from a quiet minute)."""
    rng = random.Random(seed)
    rows: list[CandleRow] = []
    close = D(first)
    for m in range(minutes):
        open_ns = start_ns + m * MINUTE_NS
        minute_of_day = (open_ns // MINUTE_NS) % 1440
        if m % 701 == 700 or minute_of_day < 5:
            rows.append(CandleRow(open_ns + MINUTE_NS, close, close, close, close, D(0), True))
            continue
        o = close
        close = max(D(100), o + rng.randint(-20 + drift, 20 + drift))
        rows.append(CandleRow(open_ns + MINUTE_NS, o, max(o, close) + 1, min(o, close) - 1, close, D(2), False))
    return rows


def _write(catalog: Path, coin: str, seed: int, first: int, drift: int) -> None:
    tree = candle_tree(catalog, coin)
    start = midnight_ns(HISTORY_FROM)
    end = midnight_ns(LAST + timedelta(days=1))
    rows = _walk(seed, start, (end - start) // MINUTE_NS, first, drift)
    by_month: dict[Month, list[CandleRow]] = {}
    for row in rows:
        by_month.setdefault(Month.of_ns(row.open_ns), []).append(row)
    for month, month_rows in by_month.items():
        tree.write_bars(month, month_rows)
        charges = [t for t in range(month.start_ns, month.next().start_ns, HOUR_NS) if t >= midnight_ns(FIRST)]
        if charges:
            # A high rate (1.2% a day), so a book that sized from a balance without funding would show it.
            tree.write_funding(month, [FundingRow(t, D("0.0005"), HOUR_NS, "api") for t in charges])


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    root = tmp_path_factory.mktemp("book")
    _write(root, BTC, 1, 60000, 1)
    _write(root, ETH, 2, 3000, -1)
    return root


@pytest.fixture(scope="module")
def data(catalog):
    return {coin: load_coin(candle_tree(catalog, coin)) for coin in (BTC, ETH)}


def _pure(data, band):
    params = PortfolioParams(
        lookback_days=TREND_VOL.lookback_days,
        vol_days=TREND_VOL.vol_days,
        vol_target=TREND_VOL.vol_target,
        gross_cap=TREND_VOL.gross_cap,
        min_history_days=TREND_VOL.min_history_days,
    )
    book = BookVolParams(TREND_VOL.book_vol_target, TREND_VOL.vol_days, TREND_VOL.gross_cap, band)
    config = ReplayConfig(0, FIRST, LAST, equity=EQUITY, cost=COST, keep_days=TREND_VOL.seed_days)
    return replay_book(data, trend_vol_planner(params, book), config)


def test_daily_seed_is_each_midnight_close_before_the_first_day():
    hourly = {midnight_ns(date(2026, 1, 1) + timedelta(days=i)): D(100 + i) for i in range(10)}
    hourly[midnight_ns(date(2026, 1, 3)) + HOUR_NS] = D(1)  # not a midnight: ignored
    seed = daily_seed(hourly, date(2026, 1, 6), 3)
    # The 00:00 close of 01-06 is 01-05's close; nothing after it is taken.
    assert seed == (
        DailyClose(date(2026, 1, 3), D(103)),
        DailyClose(date(2026, 1, 4), D(104)),
        DailyClose(date(2026, 1, 5), D(105)),
    )


def test_trend_vol_is_a_book_strategy_only():
    # ``lab backtest`` and ``lab study`` run one instance per coin; the book mode has its own registry.
    assert "trend_vol" in BOOK_REGISTRY
    assert "trend_vol" not in REGISTRY
    with pytest.raises(Exception, match="not a book strategy"):
        book_entry("tsmom")


def test_book_entry_builds_the_trader_settings_with_seeds():
    entry = book_entry("trend_vol")
    context = RunContext(
        env=Env.DEMO,
        run_id=uuid.UUID(int=1),
        instrument_id=BTC,
        seed=0,
        risk=BOOK_RISK,
        order_id_tag=entry.order_id_tag,
        taker_fee=COST,
        lab="candle",
        instruments=(BTC, ETH),
    )
    config = entry.entry.build_config(dict(entry.config), context)
    assert isinstance(config, TrendVolConfig)
    # The TV_* constants (spec 0033 AC-3), the same as the pure replay's defaults.
    assert (config.lookback_days, config.vol_days, config.min_history_days, config.seed_days) == (30, 30, 120, 150)
    assert (config.vol_target, config.gross_cap) == (TREND_VOL.vol_target, TREND_VOL.gross_cap)
    assert (config.book_vol_target, config.rebalance_band) == (TREND_VOL.book_vol_target, TREND_VOL.rebalance_band)
    assert (config.run_at_utc, config.start_cooldown_s, config.instruments) == ("00:05", 3600, (BTC, ETH))
    assert config.order_id_tag == "TV"
    seeds = {BTC: (DailyClose(date(2026, 1, 1), D(1)), DailyClose(date(2026, 1, 2), D(2)))}
    gate = PauseGate()
    strategy = entry.build(config, gate, EntryBlocks(), lambda item: None, seeds, None)
    assert isinstance(strategy, TrendVol)
    assert strategy.closes_until(date(2026, 1, 2))[BTC] == list(seeds[BTC])


def test_the_independent_copy_imports_nothing_from_the_project():
    path = Path(lab.book.__file__).resolve().parents[2] / "crosscheck"
    tree = ast.parse((path / "book_independent.py").read_text(encoding="utf-8"))
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert names
    assert not [n for n in names if n.split(".")[0] in ("trading_core", "lab", "nautilus_trader")]


@pytest.mark.parametrize("band", [D("0.3"), D(0)])
def test_the_independent_copy_matches_the_pure_replay(data, band):
    pure = _pure(data, band)
    other = independent_days(data, FIRST, LAST, EQUITY, COST, band)
    assert [d.day for d in other] == [d.day for d in pure.days]
    assert [d.equity for d in other] == [d.equity for d in pure.days]
    assert [dict(d.positions) for d in other] == [dict(d.positions) for d in pure.days]
    assert any(q != 0 for d in pure.days for q in d.positions.values())


def test_nautilus_book_mode_matches_the_pure_replay(catalog, data):
    band = D("0.3")
    pure = _pure(data, band)
    plan = BookModePlan(
        strategy="trend_vol",
        candles_root=catalog / "candles",
        coins=(BTC, ETH),
        first=FIRST,
        last=LAST,
        equity=EQUITY,
        cost=COST,
        config={"rebalance_band": str(band)},
        seeds={coin: daily_seed(d.hourly, FIRST, TREND_VOL.seed_days) for coin, d in data.items()},
        funding={coin: d.funding for coin, d in data.items()},
    )
    mode = run_book_mode(plan)
    assert [d.day for d in mode.days] == [d.day for d in pure.days]
    # The start rebalance opened both coins on the first day, though 00:00 to 00:04 were quiet minutes.
    assert all(q != 0 for q in mode.days[0].positions.values())
    assert mode.decisions > 0
    assert mode.funding != 0
    reference = [BookDay(d.day, d.equity, dict(d.positions)) for d in pure.days]
    result = compare_books(reference, list(mode.days), EQUITY, band, {d.day: d.band_gaps for d in pure.days})
    assert result.passed, result
    # Funding is booked into the account, so the strategy sizes from equity net of funding, as the pure
    # replay does: the books stay within a few bps, not just within the pass rule.
    assert result.worst_day_bps < 3, result

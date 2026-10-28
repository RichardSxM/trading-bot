"""covers: spec 0022 AC-3, AC-4, AC-4b, AC-5, AC-6, AC-7, AC-8 (one rule on two coins in one engine and account)."""

import json
import tomllib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from nautilus_trader.model.data import FundingRateUpdate, QuoteTick, TradeTick

from lab.actors import LIQUIDATED, RANGE_END, RUINED
from lab.candle_backtest import backtest
from lab.candle_fetch import candle_tree
from lab.candle_stream import CandleStream, SideFlag, merged_chunks
from lab.candle_units import CandleUnitPlan, run_candle_unit
from lab.runfile import Overrides, RunFileError, candle_run_from_raw, load_candle_run
from lab.strategies import MarketContextValues, registry_entry
from lab.study import load_study
from trading_core.lab.candles import MINUTE_NS, CandleGap, CandleRow, FundingRow, Month, MonthManifest
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.enums import Intent, OrderSide, RiskVerdict
from trading_core.risk.check import RiskLimits

from .conftest import MemoryStore, worker_settings
from .test_determinism import _differences, _without_varying

D = Decimal
BTC = "PF_XBTUSD.KRAKEN"
ETH = "PF_ETHUSD.KRAKEN"
SEP = Month(2026, 9)
T0 = SEP.start_ns
HOURS = 3
STUDIES = Path(__file__).parents[1] / "studies"
RISK = RiskLimits(
    max_order_notional=D(20000),
    max_leverage=D(2),
    max_total_exposure=D(20000),
    risk_per_trade=D("0.005"),
    maintenance_margin_rate=D("0.01"),
    min_liq_distance_pct=D(10),
    liq_stop_multiple=D(3),
    stop_slippage_bps=D(5),
)
RUN = """
instrument_ids = ["PF_XBTUSD.KRAKEN", "PF_ETHUSD.KRAKEN"]
from = 2026-09-01
to = 2026-09-01
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"
max_total_exposure = "20000"

[strategy]
name = "buy_and_hold"
[strategy.config]
notional_usd = "5000"
"""


def _btc(m, *, low=None):
    o = D(60000 + m)
    return CandleRow(T0 + (m + 1) * MINUTE_NS, o, o + 5, o - 5 if low is None else D(low), o + 1, D(1), False)


def _eth(m, *, low=None):
    o = D(3000) + D(m) / 10
    bottom = o - D("0.5") if low is None else D(low)
    return CandleRow(T0 + (m + 1) * MINUTE_NS, o, o + D("0.5"), bottom, o + D("0.1"), D(1), False)


def _crashed(row, flat, minutes=range(71, HOURS * 60)):
    """``row`` (minute 70, the crash) and then flat minutes at ``flat`` to the range end."""
    flat = D(flat)
    return [row] + [CandleRow(T0 + (m + 1) * MINUTE_NS, flat, flat + 5, flat - 5, flat, D(1), False) for m in minutes]


def _write(root, coin, rows, *, rate="0.0001", hours=HOURS, gaps=()):
    """One coin's month: candles, hourly funding at ``rate``, gaps, and a committed manifest."""
    tree = candle_tree(root, coin)
    tree.write_bars(SEP, rows)
    tree.write_funding(SEP, [FundingRow(T0 + h * HOUR_NS, D(rate), HOUR_NS, "api") for h in range(hours + 1)])
    tree.write_gaps(SEP, list(gaps))
    manifest = MonthManifest(
        "2026-09", len(rows), 0, 0, 0, 0, 0, [], rows[0].ts_ns, rows[-1].ts_ns, 0, 0, 0, False, "2026-09-02T00:00:00Z"
    )
    tree.write_manifest(manifest.with_funding(tree.read_funding(SEP)))
    return tree


def _plan(root, **overrides):
    plan = CandleUnitPlan(
        variant="base",
        candles_root=root / "candles",
        instrument_ids=(ETH, BTC),
        start_ns=T0,
        end_ns=T0 + HOURS * HOUR_NS,
        maker_fee=D("0.0002"),
        taker_fee=D("0.0005"),
        slippage_bps=D(2),
        starting_balance_usd=D(10000),
        leverage=D(2),
        risk=RISK,
        seed=7,
        strategy_name="buy_and_hold",
        strategy_config={"notional_usd": "5000"},
        stale_quote_s=D(300),
        market=MarketContextValues(max_age_funding_s=18_000, liquidations_available=False),
    )
    return replace(plan, **overrides)


def _raw(text=RUN, **changes):
    raw = tomllib.loads(text)
    for key, value in changes.items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    return raw


def _load(raw):
    return candle_run_from_raw("run.toml", b"", raw, Overrides())


# AC-3: the run file ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "problem"),
    [
        ({"instrument_id": BTC}, "exactly one of instrument_id or instrument_ids"),
        ({"instrument_ids": None}, "exactly one of instrument_id or instrument_ids"),
        ({"instrument_ids": [BTC]}, "at least 2 coins"),
        ({"instrument_ids": [BTC, BTC]}, "names a coin twice"),
        ({"instrument_ids": [BTC, "PF_SOLUSD.KRAKEN"]}, "no pinned instrument for PF_SOLUSD.KRAKEN"),
    ],
)
def test_a_run_file_names_its_coins_exactly_one_way(changes, problem):
    with pytest.raises(RunFileError) as caught:
        _load(_raw(**changes))
    assert any(problem in p for p in caught.value.problems), caught.value.problems


def test_an_unpinned_single_coin_is_refused_at_load():
    with pytest.raises(RunFileError, match=r"no pinned instrument for PF_SOLUSD\.KRAKEN"):
        _load(_raw(instrument_ids=None, instrument_id="PF_SOLUSD.KRAKEN"))


def test_coins_load_sorted_and_only_a_multi_coin_version_carries_them():
    multi = _load(_raw()).run
    single = _load(_raw(instrument_ids=None, instrument_id=BTC)).run
    assert multi.instruments == (ETH, BTC)
    assert multi.multi
    assert single.instruments == (BTC,)
    assert not single.multi
    # The key not given is left out of every dump, so a single coin dump is unchanged.
    assert "instrument_ids" not in single.model_dump()
    assert single.model_dump()["instrument_id"] == BTC
    assert "instrument_id" not in multi.model_dump()
    assert multi.model_dump()["instrument_ids"] == (ETH, BTC)
    assert single.version_config({}) == {"notional_usd": "5000"}
    assert multi.version_config({}) == {"notional_usd": "5000", "instrument_ids": [ETH, BTC]}
    entry = registry_entry("buy_and_hold")
    frozen_multi, frozen_single = entry.frozen(multi.version_config({})), entry.frozen(single.version_config({}))
    assert frozen_multi is not None
    assert frozen_single is not None
    assert frozen_multi.frozen_hash != frozen_single.frozen_hash
    with pytest.raises(ValueError, match="instrument_ids is not supported here"):
        _ = multi.single_instrument


# The base commit's values (bed1415, recorded before any spec 0022 change to the run file).
DONCHIAN = {"atr_floor_mult": "1", "atr_period": 14, "bar_spec": "4-HOUR-LAST", "time_stop_bars": 60}
MACD = {"bar_spec": "1-HOUR-LAST", "notional_usd": "10000", "signal": 9, "slow": 26, "stop_pct": "3", "trend_ema": 50}


@pytest.mark.parametrize(
    ("study", "plan_hash", "configs"),
    [
        (
            "donchian_4h.toml",
            "d5da6f1c83c5c0e07c163252b94ebe1b2d4b08560cb0ab7b0ab02b06d8d46c8b",
            {
                f"grid-{n}": {**DONCHIAN, "entry_bars": bars, "stop_atr": stop}
                for n, (bars, stop) in enumerate([(20, "2"), (20, "3"), (55, "2"), (55, "3")], start=1)
            },
        ),
        (
            "macd_baseline.toml",
            "faff6c07ecf19c7060e3a50e2f7b80d1040126fa680d246dde8f9e95ae6c85e1",
            {"grid-1": {**MACD, "fast": 8}, "grid-2": {**MACD, "fast": 12}},
        ),
    ],
)
def test_committed_single_coin_studies_plan_and_version_as_before(study, plan_hash, configs, tmp_path):
    loaded = load_study(STUDIES / study, catalog_dir=tmp_path)
    assert loaded.plan_hash == plan_hash
    run = loaded.loaded.run
    assert {name: run.version_config(point) for name, point in loaded.points} == configs


# AC-7: the merged stream ----------------------------------------------------------------------


def test_the_merged_stream_orders_equal_times_by_sorted_id_and_funding_by_charge_time(tmp_path):
    # ETH misses minutes 2 and 3; its 01:00 funding row lies in that gap and still goes in at 01:00.
    btc = _write(tmp_path, BTC, [_btc(m) for m in range(HOURS * 60)])
    eth = _write(tmp_path, ETH, [_eth(m) for m in range(HOURS * 60) if m not in (59, 60, 61)])
    end = T0 + HOURS * HOUR_NS
    streams = [CandleStream(t, T0, end, D(2), SideFlag()) for t in (eth, btc)]
    events = [e for chunk in merged_chunks(streams) for e in chunk]

    times = [e.ts_init for e in events]
    assert times == sorted(times)
    coins = [e.instrument_id.value for e in events]
    # At each equal time ETH comes first (sorted ids), and inside one coin the trade comes before its quote.
    first_minute = [(e.ts_init, e.instrument_id.value, type(e).__name__) for e in events[:4]]
    assert first_minute == [
        (T0 + 1_000_000, ETH, "TradeTick"),
        (T0 + 1_000_000, ETH, "QuoteTick"),
        (T0 + 1_000_000, BTC, "TradeTick"),
        (T0 + 1_000_000, BTC, "QuoteTick"),
    ]
    funding = [(e.ts_init, e.instrument_id.value) for e in events if isinstance(e, FundingRateUpdate)]
    assert (T0 + HOUR_NS, ETH) in funding
    assert (T0 + HOUR_NS, BTC) in funding
    assert funding == sorted(funding, key=lambda f: (f[0], f[1] != ETH))
    # Every point of each coin is sent once, and the stream counts its own events.
    assert coins.count(ETH) == streams[0].events
    assert coins.count(BTC) == streams[1].events
    quotes = [e for e in events if isinstance(e, QuoteTick)]
    trades = [e for e in events if isinstance(e, TradeTick)]
    assert len(trades) == 4 * (HOURS * 60 * 2 - 3)
    assert len(quotes) == len(trades) + 2  # plus one final quote per coin


def test_each_coin_builds_its_minute_from_its_own_side_flag(tmp_path):
    btc = _write(tmp_path, BTC, [_btc(m) for m in range(2)], hours=1)
    eth = _write(tmp_path, ETH, [_eth(m) for m in range(2)], hours=1)
    flags = SideFlag(), SideFlag(side=1)  # BTC long, ETH flat
    streams = [CandleStream(t, T0, T0 + HOUR_NS, D(0), f) for t, f in zip((eth, btc), flags, strict=True)]
    chunk = next(merged_chunks(streams))
    prices = {
        c: [e.price.as_decimal() for e in chunk if isinstance(e, TradeTick) and e.instrument_id.value == c]
        for c in (ETH, BTC)
    }
    assert prices[BTC] == [D(60000), D(59995), D(60005), D(60001)]  # long: the low first
    assert prices[ETH] == [D(3000), D("3000.5"), D("2999.5"), D("3000.1")]  # flat: the nearer extreme first


# AC-4, AC-5, AC-6, AC-8: the unit ---------------------------------------------------------------


def test_two_coins_trade_in_one_unit_each_row_under_its_own_coin(tmp_path):
    _write(tmp_path, BTC, [_btc(m) for m in range(HOURS * 60)])
    _write(tmp_path, ETH, [_eth(m) for m in range(HOURS * 60)])
    result = run_candle_unit(_plan(tmp_path))

    decisions = [d for _, d in result.decisions]
    assert sorted((d.instrument_id, d.intent, d.reason) for d in decisions) == [
        (ETH, Intent.ENTER, "buy_and_hold_entry"),
        (ETH, Intent.EXIT, RANGE_END),
        (BTC, Intent.ENTER, "buy_and_hold_entry"),
        (BTC, Intent.EXIT, RANGE_END),
    ]
    fills = [f for _, f in result.fills]
    assert sorted((f.instrument_id, f.side) for f in fills) == [
        (ETH, OrderSide.BUY),
        (ETH, OrderSide.SELL),
        (BTC, OrderSide.BUY),
        (BTC, OrderSide.SELL),
    ]
    # Each order and position is recorded once, under its own coin, and the strategy tags keep them apart.
    orders = [o for _, o in result.orders]
    assert len({o.client_order_id for o in orders}) == len(orders) == 4
    assert {o.instrument_id for o in orders if "C1" in o.client_order_id} == {ETH}
    assert {o.instrument_id for o in orders if "C2" in o.client_order_id} == {BTC}
    assert sorted(p.instrument_id for _, p in result.positions) == [ETH, BTC]
    assert all(p.quantity == 0 for _, p in result.positions)
    # Funding samples per coin, keys sorted, each with that coin's own position and mid (AC-8).
    assert list(result.funding_samples) == [ETH, BTC]
    eth_at_2h = {s.boundary_ns: s for s in result.funding_samples[ETH]}[T0 + 2 * HOUR_NS]
    btc_at_2h = {s.boundary_ns: s for s in result.funding_samples[BTC]}[T0 + 2 * HOUR_NS]
    assert eth_at_2h.mid == D("3012.0")
    assert btc_at_2h.mid == D(60120)
    assert eth_at_2h.position is not None
    assert btc_at_2h.position is not None
    assert eth_at_2h.position != btc_at_2h.position
    assert result.events > 0
    assert result.first_trade_ns == T0
    assert result.last_point_ns == T0 + HOURS * HOUR_NS - 1_000_000


def test_a_gap_blocks_only_its_coin_and_the_shared_exposure_cap_counts_every_coin(tmp_path):
    """AC-5 and AC-6: ETH's minutes 52 to 59 are missing, so its 01:00 entry is refused as stale while
    BTC enters. At 02:00 ETH's entry sees BTC's position in the shared open notional and is refused by
    the 15000 exposure cap, as a single coin's would be: buy_and_hold asks for a fixed quantity, which a
    cap refuses (a stop sized entry, such as donchian_4h's, is shrunk to the cap instead)."""
    _write(tmp_path, BTC, [_btc(m) for m in range(HOURS * 60)])
    _write(tmp_path, ETH, [_eth(m) for m in range(HOURS * 60) if not 52 <= m < 60])
    risk = replace(RISK, max_total_exposure=D(15000))
    result = run_candle_unit(_plan(tmp_path, risk=risk, strategy_config={"notional_usd": "10000"}))

    entries = {(d.instrument_id, d.decided_ns): d for _, d in result.decisions if d.intent == Intent.ENTER}
    stale = entries[ETH, T0 + HOUR_NS]
    assert (stale.risk_verdict, stale.risk_reason) == (RiskVerdict.REJECTED, "blocked:stale_feed")
    btc = entries[BTC, T0 + HOUR_NS]
    assert (btc.risk_verdict, btc.risk_reason) == (RiskVerdict.APPROVED, None)
    capped = entries[ETH, T0 + 2 * HOUR_NS]
    assert (capped.risk_verdict, capped.risk_reason) == (RiskVerdict.REJECTED, "exposure_cap")
    [btc_buy] = [f for _, f in result.fills if f.instrument_id == BTC and f.side == OrderSide.BUY]
    # BTC's open position at its 02:00 mid (minute 119's close point), counted in ETH's check.
    assert D(str(capped.inputs["open_notional"])) == btc_buy.quantity * D(60120)
    assert not [f for _, f in result.fills if f.instrument_id == ETH]


def test_ruin_on_one_coin_closes_every_coin_and_reports_ruin(tmp_path):
    """AC-4b: both coins fall 75 % in minute 70. Neither alone reaches its liquidation price (each entry
    is judged on the whole 10000 equity), but together they take the shared equity below zero: ETH's
    fall comes first (sorted ids) and leaves equity above zero, BTC's then ends it, and the shared stop
    closes both coins as ruined before it stops the engine."""
    _write(tmp_path, BTC, [_btc(m) for m in range(70)] + _crashed(_btc(70, low=15000), 15300))
    _write(tmp_path, ETH, [_eth(m) for m in range(70)] + _crashed(_eth(70, low=750), 765))
    result = run_candle_unit(_plan(tmp_path, strategy_config={"notional_usd": "9500"}))

    crash_at = T0 + 70 * MINUTE_NS + 20 * 1_000_000_000
    exits = sorted((d.instrument_id, d.reason, d.decided_ns) for _, d in result.decisions if d.intent == Intent.EXIT)
    assert exits == [(ETH, RUINED, crash_at), (BTC, RUINED, crash_at)]
    assert not any(d.reason == LIQUIDATED for _, d in result.decisions)
    assert result.ruined_ns == crash_at
    closes = {f.instrument_id: f.price for _, f in result.fills if f.side == OrderSide.SELL}
    assert closes[ETH] < D(800)
    assert closes[BTC] < D(16000)  # each at its own coin's last quote
    assert all(p.quantity == 0 for _, p in result.positions)


# AC-7, AC-8: the batch ---------------------------------------------------------------------------


def _two_coin_day(catalog):
    """2026-09-01 for both coins, ETH with a 10 minute gap row and a funding rate three times BTC's."""
    day = 24 * 60
    _write(catalog, BTC, [_btc(m) for m in range(day)], hours=24)
    gap = CandleGap(T0 + 600 * MINUTE_NS, T0 + 610 * MINUTE_NS, 10)
    eth_rows = [_eth(m) for m in range(day) if not 600 <= m < 610]
    _write(catalog, ETH, eth_rows, rate="0.0003", hours=24, gaps=[gap])


def test_a_two_coin_backtest_charges_each_coin_its_own_funding_and_reports_gaps_per_coin(tmp_path):
    _two_coin_day(tmp_path / "catalog")
    path = tmp_path / "run.toml"
    path.write_text(RUN, encoding="utf-8")
    store = MemoryStore()
    loaded = load_candle_run(path, Overrides())
    assert backtest(worker_settings(tmp_path / "catalog", tmp_path / "reports", 1), loaded, store) == 0

    [bundle] = store.bundles
    config = bundle.run.run_config
    assert config["instrument_ids"] == [ETH, BTC]
    assert "instrument_id" not in config
    assert config["candle_gaps"] == {ETH: [[T0 + 600 * MINUTE_NS, T0 + 610 * MINUTE_NS, 10]], BTC: []}
    by_id = {p.id: p for p in bundle.positions}
    assert sorted(p.instrument_id for p in bundle.positions) == [ETH, BTC]
    assert bundle.funding
    for row in bundle.funding:
        assert by_id[row.position_id].instrument_id == row.instrument_id
        assert row.rate == (D("0.0003") if row.instrument_id == ETH else D("0.0001"))
    # Funding rows come coin by coin in sorted order.
    coins = [f.instrument_id for f in bundle.funding]
    assert coins == sorted(coins, key=lambda c: c != ETH)
    for position in bundle.positions:
        own = sum((f.amount for f in bundle.funding if f.position_id == position.id), D(0))
        assert position.funding_total == own


def test_the_same_two_coin_backtest_twice_gives_the_same_reports_and_bundle(tmp_path):
    _two_coin_day(tmp_path / "catalog")
    path = tmp_path / "run.toml"
    path.write_text(RUN, encoding="utf-8")
    loaded = load_candle_run(path, Overrides())
    files = []
    for name in ("one", "two"):
        assert backtest(worker_settings(tmp_path / "catalog", tmp_path / name, 1), loaded, MemoryStore()) == 0
        [folder] = list((tmp_path / name).iterdir())
        assert sorted(p.name for p in folder.iterdir()) == ["base.bundle.json", "base.json", "base.md", "index.md"]
        files.append(
            {p.name: _without_varying(json.loads(p.read_text(encoding="utf-8"))) for p in folder.glob("*.json")}
        )
    assert _differences(files[0], files[1]) == []
    assert files[0]["base.json"]["results"]["trade_count"] == 2

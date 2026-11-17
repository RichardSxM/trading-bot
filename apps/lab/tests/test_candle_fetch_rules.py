"""covers: spec 0033 AC-9a (the pin keys, the loader, a finer tick month on disk, the refusal)."""

from decimal import Decimal

import pytest

from lab.candle_fetch import candle_tree
from lab.instruments import fetch_rules, on_tick, pinned_instrument
from trading_core.catalog.candles import CandleTree
from trading_core.lab.candles import MINUTE_NS, CandleRow, FetchRules, FetchRulesError, Month

D = Decimal
COINS = [
    "PF_XBTUSD.KRAKEN",
    "PF_ETHUSD.KRAKEN",
    "PF_SOLUSD.KRAKEN",
    "PF_XRPUSD.KRAKEN",
    "PF_ADAUSD.KRAKEN",
    "PF_LINKUSD.KRAKEN",
    "PF_UNIUSD.KRAKEN",
    "PF_ZECUSD.KRAKEN",
    "PF_HYPEUSD.KRAKEN",
    "PF_PEPEUSD.KRAKEN",
]


@pytest.mark.parametrize("coin", ["PF_DOGEUSD.KRAKEN", "PF_TRXUSD.KRAKEN"])
def test_doge_and_trx_carry_the_old_tick_until_2026_08_20_0722_utc(coin):
    rules = fetch_rules(coin)
    assert [(p.tick, p.until_ns) for p in rules.ticks] == [(D("0.000001"), 1787210520 * 1_000_000_000)]
    assert rules.drop == ()
    assert pinned_instrument(coin).price_increment.as_decimal() == D("0.00001")


def test_qnt_drops_the_21_glitch_minutes():
    (span,) = fetch_rules("PF_QNTUSD.KRAKEN").drop
    assert span.from_ns == 1692308640 * 1_000_000_000
    assert (span.until_ns - span.from_ns) // MINUTE_NS == 21
    assert fetch_rules("PF_QNTUSD.KRAKEN").ticks == ()


@pytest.mark.parametrize("coin", COINS)
def test_the_other_coins_have_no_fetch_rules(coin):
    assert fetch_rules(coin) == FetchRules()


def test_a_month_on_a_finer_tick_keeps_its_six_decimals_on_disk(tmp_path):
    """Written through a tree on the old tick copy (the shared catalog writer stays untouched, AC-4)."""
    coin = "PF_DOGEUSD.KRAKEN"
    finer = on_tick(coin, D("0.000001"))
    assert (finer.price_precision, finer.price_increment.as_decimal()) == (6, D("0.000001"))
    assert finer.size_increment == pinned_instrument(coin).size_increment
    tree = candle_tree(tmp_path, coin)
    month = Month(2026, 8)
    rows = [
        CandleRow(month.start_ns + MINUTE_NS, D("0.123451"), D("0.123459"), D("0.123441"), D("0.123452"), D(5), False)
    ]
    CandleTree(tree.root, finer).write_bars(month, rows)
    (back,) = tree.read_bars(month)
    assert (back.open, back.high, back.low, back.close) == (D("0.123451"), D("0.123459"), D("0.123441"), D("0.123452"))
    tree.write_bars(month, rows)
    assert tree.read_bars(month)[0].close == D("0.12345")


def test_a_malformed_pin_rule_exits_64_before_booting(monkeypatch, capsys):
    from lab.main import main

    def bad(_):
        raise FetchRulesError("_tick_history must be in time order")

    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setattr("lab.main.fetch_rules", bad)
    monkeypatch.setattr("lab.main.boot", lambda *a, **k: pytest.fail("booted"))
    with pytest.raises(SystemExit) as exit_:
        main(["fetch-candles", "2026-09", "2026-09", "--instrument", "PF_DOGEUSD.KRAKEN"])
    assert exit_.value.code == 64
    assert "time order" in capsys.readouterr().err

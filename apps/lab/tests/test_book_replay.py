"""covers: spec 0033 AC-9, ``lab book replay``: the candles tree loaded as whole hour closes, H:05 fills, and
funding rates, and the command's report, exit codes, and input checks.
"""

import json
from decimal import Decimal

from lab.book import BOOK_COINS, load_coin, run_book_replay
from lab.main import parser
from trading_core.lab.book_replay import FILL_DELAY_NS
from trading_core.lab.candles import MINUTE_NS
from trading_core.marketdata.hours import HOUR_NS

from .candle_fixtures import FEB, HOLE, synthetic_month
from .conftest import lab_settings


def test_twelve_coins_are_pinned():
    from lab.instruments import pinned_instrument

    assert len(BOOK_COINS) == 12
    for coin in BOOK_COINS:
        assert str(pinned_instrument(coin).id) == coin


def test_load_coin_keeps_whole_hours_and_h05(tmp_path):
    tree = synthetic_month(tmp_path, days=3)
    rows = {r.ts_ns: r.close for r in tree.read_bars(FEB)}
    data = load_coin(tree)
    assert data.hourly
    assert data.minutes
    assert all(ns % HOUR_NS == 0 for ns in data.hourly)
    assert all(ns % HOUR_NS == FILL_DELAY_NS for ns in data.minutes)
    assert all(data.hourly[ns] == rows[ns] for ns in data.hourly)
    assert all(data.minutes[ns] == rows[ns] for ns in data.minutes)
    # The 10 minute hole (minutes 5000 to 5009 from the month start) drops the 83:20.. minutes, not whole hours.
    hole = {FEB.start_ns + (m + 1) * MINUTE_NS for m in HOLE}
    assert not hole & (set(data.hourly) | set(data.minutes))
    assert len(data.funding) == 3 * 24 + 1
    assert data.spec.size_increment == Decimal("0.0001")


def test_command_writes_a_report(tmp_path, capsys):
    synthetic_month(tmp_path / "catalog", days=10)
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")
    args = parser().parse_args(
        ["book", "replay", "--coins", "PF_XBTUSD.KRAKEN", "--from", "2026-02-02", "--to", "2026-02-09", "--name", "t"]
    )
    assert run_book_replay(settings, args) == 0
    body = json.loads((tmp_path / "reports" / "book" / "t.json").read_text())
    assert body["inputs"]["strategy"] == "trend_vol"
    assert body["inputs"]["band"] == "0.3"
    assert len(body["daily"]) == 8
    # 10 days of history never reach 120, so nothing is planned and nothing trades.
    assert body["totals"]["traded"] == 0
    assert body["totals"]["unplanned_days"] == 8
    assert "half 2" in capsys.readouterr().out


def test_command_refuses_bad_input(tmp_path):
    settings = lab_settings(tmp_path / "catalog", tmp_path / "reports")
    bad_coin = parser().parse_args(["book", "replay", "--coins", "PF_NOPEUSD.KRAKEN"])
    assert run_book_replay(settings, bad_coin) == 64
    bad_hour = parser().parse_args(["book", "replay", "--coins", "PF_XBTUSD.KRAKEN", "--cut-hour", "24"])
    assert run_book_replay(settings, bad_hour) == 64
    no_data = parser().parse_args(["book", "replay", "--coins", "PF_ETHUSD.KRAKEN"])
    assert run_book_replay(settings, no_data) == 2

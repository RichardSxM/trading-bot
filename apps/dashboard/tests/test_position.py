"""covers: spec 0006 AC-17, Overview's position card (open, flat, offline never flat); spec 0011 AC-18
(P&L net of funding, the funding an open position paid so far); spec 0025 AC-21 (one row per coin from the
``positions`` snapshot, ``unknown`` while a coin has no quote, each row's size in its own base)."""

import asyncio
import logging
from decimal import Decimal

import pytest

from dashboard.data.pnl import PnlFigures
from dashboard.live.poller import POSITION_EVENT, Poller
from dashboard.live.position import CoinRow, PositionView, position_view
from trading_core.snapshot.envelope import encode

from .conftest import log_in, make_app

S = 1_000_000_000
NOW = 1_790_000_000 * S
PNL = PnlFigures(realized_today=Decimal("-0.42"), realized_all=Decimal("3.18"), fees_today=Decimal("0.51"))
OPEN = {
    "instrument_id": "PF_XBTUSD.KRAKEN",
    "base": "BTC",
    "side": "long",
    "quantity": "0.0011",
    "avg_entry_px": "64210.50",
    "mark_px": "64288.5",
    "unrealized_pnl": "0.09",
    "stop_px": "64018",
    "stop_status": "accepted",
    "opened_ns": NOW - 60 * S,
    "exit_due_ns": NOW + 540 * S,
    "currency": "USD",
}
FLAT = {"instrument_id": "PF_XBTUSD.KRAKEN", "base": "BTC", "side": "flat", "mark_px": "64288.5"}
PEPE_OPEN = {
    **OPEN,
    "instrument_id": "PF_PEPEUSD.KRAKEN",
    "base": "PEPE",
    "quantity": "81000",
    "avg_entry_px": "0.0000123456",
    "mark_px": "0.0000123461",
    "stop_px": "0.0000122221",
}
PEPE_UNKNOWN = {"instrument_id": "PF_PEPEUSD.KRAKEN", "base": "PEPE", "side": "unknown"}
ACCOUNT = {
    "balance_total": "1000.00",
    "balance_free": "990.00",
    "margin_used": "10.00",
    "equity": "1000.09",
    "currency": "USD",
}


def _positions(*coins):
    return {"coins": list(coins)}


def _card(*coins, account=ACCOUNT, age_s=1, pnl=PNL, raw=None):
    raw_positions = raw if raw is not None or not coins else encode(NOW - age_s * S, _positions(*coins))
    raw_account = None if account is None else encode(NOW - age_s * S, account)
    return position_view(raw_positions, raw_account, pnl, NOW, amber_s=3, red_s=10)


def _view(position, account=ACCOUNT, age_s=1, pnl=PNL):
    """The one row of a card with one coin."""
    (row,) = _card(position, account=account, age_s=age_s, pnl=pnl).rows
    return row


def test_an_open_position_reads_as_decimals():
    card = _card(OPEN)
    (view,) = card.rows

    assert (card.status, view.status) == ("live", "open")
    assert (view.side, view.quantity, view.avg_entry, view.mark) == (
        "long",
        Decimal("0.0011"),
        Decimal("64210.50"),
        Decimal("64288.5"),
    )
    assert (view.stop_px, view.stop_status, view.unrealized) == (Decimal(64018), "accepted", Decimal("0.09"))
    assert (view.exit_due_ns, view.base) == (NOW + 540 * S, "BTC")
    assert (card.equity, card.pnl) == (Decimal("1000.09"), PNL)


def test_flat_shows_the_mark_only():
    view = _view(FLAT)

    assert (view.status, view.mark, view.side) == ("flat", Decimal("64288.5"), None)


@pytest.mark.parametrize("age_s", [11, 60])
def test_a_snapshot_past_the_red_limit_is_offline_never_flat(age_s):
    card = _card(FLAT, OPEN, age_s=age_s)

    assert (card.status, card.rows) == ("offline", ())


def test_a_missing_snapshot_is_offline():
    assert _card().status == "offline"


def test_two_coins_give_an_open_row_and_a_flat_row_in_the_snapshots_order():
    card = _card(FLAT, PEPE_OPEN)

    assert card.status == "live"
    assert [(row.base, row.status) for row in card.rows] == [("BTC", "flat"), ("PEPE", "open")]
    assert card.any_open


def test_a_pepe_price_keeps_its_digits():
    (_, pepe) = _card(FLAT, PEPE_OPEN).rows

    assert (pepe.avg_entry, pepe.quantity) == (Decimal("0.0000123456"), Decimal(81000))


def test_a_coin_without_a_quote_is_unknown_and_the_other_coin_still_shows():
    btc, pepe = _card(FLAT, PEPE_UNKNOWN).rows

    assert pepe == CoinRow("unknown", "PF_PEPEUSD.KRAKEN", "PEPE")
    assert (btc.status, btc.mark) == ("flat", Decimal("64288.5"))
    assert pepe.holding is None


def test_one_entry_that_does_not_parse_is_bad_on_its_row_alone():
    btc, pepe = _card(FLAT, {**PEPE_OPEN, "quantity": 1}).rows

    assert (btc.status, pepe.status, pepe.base) == ("flat", "bad", "PEPE")


@pytest.mark.parametrize("data", [{"coins": []}, {"coins": "BTC"}, OPEN])
def test_a_snapshot_without_a_list_of_coins_is_bad(data):
    assert _card(raw=encode(NOW - S, data)).status == "bad"


@pytest.mark.parametrize(
    "position",
    [
        {**OPEN, "quantity": 0.0011},  # a float, not a decimal string
        {**OPEN, "unrealized_pnl": "NaN"},
        {**OPEN, "side": "sideways"},
        {key: value for key, value in OPEN.items() if key != "avg_entry_px"},
    ],
)
def test_a_snapshot_that_does_not_parse_is_bad(position):
    assert _view(position).status == "bad"


def test_an_open_position_without_a_stop_still_shows():
    view = _view({**OPEN, "stop_px": None, "stop_status": None})

    assert (view.status, view.stop_px) == ("open", None)


def test_an_unreadable_account_only_drops_equity():
    card = position_view(encode(NOW - S, _positions(OPEN)), "garbage", PNL, NOW, 3, 10)

    assert (card.rows[0].status, card.equity) == ("open", None)


# The page and the poller ----------------------------------------------------------------


def _overview(client, clock, redis, *positions, account=ACCOUNT):
    if positions:
        redis.values["demo:snapshot:positions"] = encode(clock.ns() - S, _positions(*positions))
    if account is not None:
        redis.values["demo:snapshot:account"] = encode(clock.ns() - S, account)
    log_in(client, clock)
    return client.get("/").text


def test_overview_shows_the_open_position_and_the_figures_net_of_funding(client, clock, redis):
    page = _overview(client, clock, redis, OPEN)

    assert 'sse-swap="overview-position"' in page
    assert "before funding" not in page
    assert "Unrealized P&amp;L" in page
    assert "Paper P&amp;L, net of fees and funding" in page
    assert "Funding so far" in page
    for figure in ("64,210.50", "64,018", "accepted"):
        assert figure in page
    assert "Trader offline" not in page


def test_overview_shows_a_row_per_coin_each_sized_in_its_own_base(client, clock, redis):
    page = _overview(client, clock, redis, OPEN, PEPE_OPEN)

    assert page.count('class="position__coin position__coin--open"') == 2
    assert '0.0011</span> <span class="figures__unit">BTC</span>' in page
    assert '81,000</span> <span class="figures__unit">PEPE</span>' in page
    assert "0.0000123456" in page
    assert page.count("Funding so far") == 1


def test_overview_shows_a_coin_waiting_for_a_quote_next_to_the_other(client, clock, redis):
    page = _overview(client, clock, redis, FLAT, PEPE_UNKNOWN)

    assert "position__coin--unknown" in page
    assert "waiting for a quote" in page
    assert ">Flat<" in page


def test_overview_without_a_position_snapshot_says_offline(client, clock, redis):
    page = _overview(client, clock, redis)

    assert "Trader offline" in page
    assert ">Flat<" not in page


def test_a_postgres_failure_keeps_the_live_card(redis, clock):
    from fastapi.testclient import TestClient

    def down():
        raise ConnectionError("postgres down")

    with TestClient(make_app(redis, clock, read_pnl=down), follow_redirects=False) as client:
        page = _overview(client, clock, redis, FLAT)

    assert ">Flat<" in page
    assert "P&amp;L unavailable" in page


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _poller(redis_values, read_pnl, monotonic):
    async def get(key):
        return redis_values.get(key)

    async def none():
        return None

    return Poller(
        lambda: get("heartbeat"),
        lambda view: view.text,
        interval_s=0.5,
        amber_s=3,
        red_s=10,
        read_command=none,
        read_results=none,
        render_command=lambda view: view.text,
        render_alert=lambda view: "",
        render_activity=lambda rows: "",
        read_trader_state=none,
        render_pause=lambda view: view.text,
        hide_after_s=900,
        ack_warn_s=10,
        read_timeout_s=1.0,
        clock_ns=lambda: NOW,
        monotonic=monotonic,
        read_position=lambda: get("positions"),
        read_account=lambda: get("account"),
        read_pnl=read_pnl,
        render_position=lambda view: f"{view.status}|{view.pnl}",
    )


def test_the_poller_reads_pnl_at_most_every_refresh_and_logs_a_failure_once(caplog):
    calls = []
    fail = [False]

    async def read_pnl():
        calls.append(1)
        if fail[0]:
            raise ConnectionError("postgres down")
        return PNL

    monotonic = _Clock()
    poller = _poller({"positions": encode(NOW - S, _positions(FLAT))}, read_pnl, monotonic)

    async def run():
        first = await poller.render_now()
        monotonic.now += 1.0
        await poller.render_now()
        monotonic.now += 5.0
        fail[0] = True
        with caplog.at_level(logging.WARNING):
            failed = await poller.render_now()
            monotonic.now += 5.0
            await poller.render_now()
        return first, failed

    first, failed = asyncio.run(run())

    assert first[POSITION_EVENT] == f"live|{PNL}"
    assert len(calls) == 3  # the 1 s later tick reused the figures
    assert failed[POSITION_EVENT] == "live|None"
    assert [r.fields["code"] for r in caplog.records if hasattr(r, "fields")] == ["pnl_read_failed"]


def test_a_redis_failure_renders_the_card_lost_with_the_last_figures():
    async def read_pnl():
        return PNL

    poller = _poller({}, read_pnl, _Clock())

    async def run():
        await poller.render_now()
        return poller.no_data()

    assert asyncio.run(run())[POSITION_EVENT] == f"lost|{PNL}"


def test_a_position_view_defaults_to_usd():
    assert PositionView("live").currency == "USD"

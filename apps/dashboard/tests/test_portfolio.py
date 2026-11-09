"""covers: spec 0030 AC-19 (the Portfolio page, its empty state, live over SSE), AC-20 (one row per coin with the
signal in words, weight, target, position, unrealized P&L, and the failed marker), AC-21 (the account summary)."""

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from dashboard.live.portfolio import PortfolioRaw, next_rebalance, portfolio_view, signal_words
from dashboard.web.page_routes import event_stream
from trading_core.control.messages import ControlResult, result_to_fields
from trading_core.recording.enums import ControlOutcome
from trading_core.snapshot.envelope import encode

from .conftest import log_in, make_app

COINS = (
    "PF_XBTUSD.KRAKEN",
    "PF_ETHUSD.KRAKEN",
    "PF_SOLUSD.KRAKEN",
    "PF_XRPUSD.KRAKEN",
    "PF_ADAUSD.KRAKEN",
    "PF_DOGEUSD.KRAKEN",
    "PF_LINKUSD.KRAKEN",
    "PF_AVAXUSD.KRAKEN",
    "PF_LTCUSD.KRAKEN",
    "PF_DOTUSD.KRAKEN",
    "PF_BCHUSD.KRAKEN",
    "PF_SUIUSD.KRAKEN",
)
RUN = uuid.UUID("01a1b17a-0000-7000-8000-000000000030")
# Saturday 2026-11-07 12:00 UTC: the next Monday 00:05 UTC is 2026-11-09.
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
NOW_NS = int(NOW.timestamp()) * 1_000_000_000
LAST_REBALANCE_NS = int(datetime(2026, 11, 2, 0, 5, 3, tzinfo=UTC).timestamp()) * 1_000_000_000


def plan(failed=("PF_SOLUSD.KRAKEN",)):
    coins = []
    for i, coin in enumerate(COINS):
        eligible = coin != "PF_SUIUSD.KRAKEN"
        signal = 0 if not eligible else (1 if i % 3 == 0 else -1 if i % 3 == 1 else 0)
        coins.append(
            {
                "instrument_id": coin,
                "eligible": eligible,
                "signal": signal,
                "vol": "0.6" if eligible else None,
                "weight": "0.0416666" if signal else "0",
                "target_notional": "41.66666" if signal else "0",
                "target_qty": "0.0005" if coin == "PF_XBTUSD.KRAKEN" else "0",
                "close_date": "2026-11-06",
                "failed": coin in failed,
            }
        )
    return {
        "coins": coins,
        "n": 11,
        "gross": "0.33",
        "gross_scale": "1",
        "equity": "1000",
        "plan_day": "2026-11-06",
        "last_rebalance_at": LAST_REBALANCE_NS,
        "last_reason": "weekly",
        "failed": list(failed),
        "run_at_utc": "00:05",
        "rebalance_weekday": 0,
    }


def positions():
    btc = {
        "instrument_id": "PF_XBTUSD.KRAKEN",
        "base": "BTC",
        "side": "long",
        "quantity": "0.0005",
        "avg_entry_px": "80000",
        "mark_px": "84000",
        "stop_px": None,
        "stop_status": None,
        "unrealized_pnl": "2",
        "exit_due_ns": None,
        "currency": "USD",
    }
    eth = {**btc, "instrument_id": "PF_ETHUSD.KRAKEN", "base": "ETH", "side": "short", "quantity": "-0.01"}
    eth.update(avg_entry_px="3000", mark_px="3100", unrealized_pnl="-1")
    flats = [{"instrument_id": c, "base": None, "side": "flat", "mark_px": "1"} for c in COINS[2:]]
    return {"coins": [btc, eth, *flats]}


def snapshots(ts_ns=NOW_NS, with_plan=True):
    values = {
        "demo:snapshot:portfolio_heartbeat": encode(ts_ns, {"process": "trader"}),
        "demo:snapshot:portfolio_trader_state": encode(ts_ns, {"paused": False, "halted": False, "run_id": str(RUN)}),
        "demo:snapshot:portfolio_positions": encode(ts_ns, positions()),
        "demo:snapshot:portfolio_account": encode(ts_ns, {"equity": "1000"}),
    }
    if with_plan:
        values["demo:snapshot:portfolio_plan"] = encode(ts_ns, plan())
    return values


def raw(values, results=(), commands=()):
    return PortfolioRaw(
        values.get("demo:snapshot:portfolio_heartbeat"),
        values.get("demo:snapshot:portfolio_trader_state"),
        values.get("demo:snapshot:portfolio_plan"),
        values.get("demo:snapshot:portfolio_positions"),
        values.get("demo:snapshot:portfolio_account"),
        list(results),
        list(commands),
    )


def result_entry(command_id, outcome=ControlOutcome.EXECUTED, slot=None, at=NOW):
    fields = result_to_fields(ControlResult(command_id, outcome, at))
    if slot is not None:
        fields["slot"] = slot
    return (b"1-0", {k.encode(): v.encode() for k, v in fields.items()})


def view(values=None, **kwargs):
    return portfolio_view(raw(snapshots() if values is None else values, **kwargs), Decimal("12.5"), NOW_NS, 5, 15)


def test_twelve_coins_render_twelve_rows_in_plan_order_with_signals_in_words():
    rows = view().rows
    assert [row.instrument_id for row in rows] == list(COINS)
    assert [row.signal for row in rows[:4]] == ["Long", "Short", "Flat", "Long"]
    assert rows[-1].signal == "Not eligible"
    btc, eth, sol = rows[:3]
    assert (btc.base, btc.weight_pct, btc.target_usd, btc.target_qty) == (
        "BTC",
        Decimal("4.17"),
        Decimal("41.67"),
        Decimal("0.0005"),
    )
    assert (btc.position.side, btc.position.quantity, btc.position.unrealized) == (
        "long",
        Decimal("0.0005"),
        Decimal(2),
    )
    assert (eth.position.side, eth.position.unrealized) == ("short", Decimal(-1))
    assert (sol.base, sol.failed, sol.position.status) == ("SOL", True, "flat")
    assert not btc.failed


def test_the_summary_reads_equity_leverage_realized_rebalances_failed_and_state():
    page = view()
    assert page.status == "running"
    assert page.equity == Decimal(1000)
    # |0.0005 x 84000| + |0.01 x 3100| = 42 + 31 = 73 over 1000 equity
    assert page.gross_leverage == Decimal("0.07")
    assert page.realized == Decimal("12.5")
    assert page.last_rebalance == datetime(2026, 11, 2, 0, 5, 3, tzinfo=UTC)
    assert page.next_rebalance == datetime(2026, 11, 9, 0, 5, tzinfo=UTC)
    assert page.failed == ("SOL",)
    assert page.state_text == "running"


def test_a_paused_portfolio_says_who_paused_it():
    values = snapshots()
    values["demo:snapshot:portfolio_trader_state"] = encode(NOW_NS, {"paused": True, "pause_source": "deploy"})
    page = view(values)
    assert (page.state_text, page.state_level) == ("paused · by deploy", "warn")


def test_no_heartbeat_is_not_running_and_never_seen():
    page = view({})
    assert (page.status, page.last_seen, page.rows) == ("not_running", None, ())


def test_a_stale_heartbeat_is_not_running_with_the_time_last_seen():
    seen = NOW_NS - 60 * 1_000_000_000
    page = view(snapshots(ts_ns=seen))
    assert page.status == "not_running"
    assert page.last_seen == datetime(2026, 11, 7, 11, 59, tzinfo=UTC)


def test_the_last_command_is_the_newest_portfolio_result_named_from_its_command():
    mine, mains = uuid.uuid4(), uuid.uuid4()
    results = [result_entry(mains), result_entry(mine, ControlOutcome.FAILED, slot="portfolio")]
    commands = [(b"2-0", {b"id": str(mine).encode(), b"cmd": b"unpause"})]
    command = view(results=results, commands=commands).last_command
    assert (command.cmd, command.text, command.level, command.age) == ("unpause", "failed", "bad", "0s ago")


def test_main_results_alone_leave_no_portfolio_command():
    assert view(results=[result_entry(uuid.uuid4())]).last_command is None


def test_signal_words_never_hide_an_unknown_plan():
    assert signal_words(None, None) == "No plan yet"
    assert signal_words(True, True) == "No plan yet"  # a bool is not a signal
    assert signal_words(False, 1) == "Not eligible"


def test_next_rebalance_is_strictly_after_now():
    monday = datetime(2026, 11, 9, 0, 4, tzinfo=UTC)
    assert next_rebalance("00:05", 0, monday) == datetime(2026, 11, 9, 0, 5, tzinfo=UTC)
    assert next_rebalance("00:05", 0, monday.replace(minute=5)) == datetime(2026, 11, 16, 0, 5, tzinfo=UTC)
    assert next_rebalance("bad", 0, monday) is None
    assert next_rebalance("00:05", 7, monday) is None


# --- the page in the app


def _client(redis, clock, read_portfolio_pnl):
    client = TestClient(make_app(redis, clock, read_portfolio_pnl=read_portfolio_pnl), follow_redirects=False)
    client.__enter__()
    assert log_in(client, clock, "/portfolio").status_code == 303
    return client


def test_the_page_renders_the_rows_summary_and_realized_pnl_of_the_named_run(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(snapshots())
    asked = []

    def realized(run):
        asked.append(run)
        return Decimal("-3.25")

    client = _client(redis, clock, realized)
    try:
        page = client.get("/portfolio").text
    finally:
        client.__exit__(None, None, None)
    assert asked == [RUN]
    assert 'aria-current="page"' in page
    assert "<span>Portfolio</span>" in page
    board = page[page.index('sse-swap="portfolio-board"') :]
    assert board.count('<th scope="row">') == 12
    assert "Long" in board
    assert "Short" in board
    assert "Not eligible" in board
    assert '<span class="badge badge--bad">failed</span>' in board
    assert "2026-11-09 00:05:00Z" in board
    assert ">01:05</time> Amsterdam" in board
    assert "pnl--loss" in board  # the realized figure
    assert "Portfolio trader not running" not in board


def test_the_page_without_a_portfolio_trader_shows_the_empty_state(redis, clock):
    client = _client(redis, clock, lambda run: Decimal(0))
    try:
        page = client.get("/portfolio").text
    finally:
        client.__exit__(None, None, None)
    assert "Portfolio trader not running" in page
    assert "Never seen on this env." in page


def test_redis_down_shows_no_data_on_the_board(redis, clock):
    client = _client(redis, clock, lambda run: Decimal(0))
    try:
        redis.fail = TimeoutError()
        page = client.get("/portfolio").text
    finally:
        client.__exit__(None, None, None)
    assert "Portfolio unavailable: no data from Redis" in page


def test_the_board_is_one_of_the_live_events(redis, clock):
    clock.now = NOW.timestamp()
    redis.values.update(snapshots())
    app = make_app(redis, clock)

    async def first_events():
        poller = app.state.context.poller
        initial = await poller.render_now()
        stream = event_stream(poller, initial, clock() + 60, clock)
        return [await anext(stream) for _ in initial]

    events = asyncio.run(first_events())
    [board] = [event for event in events if event.startswith("event: portfolio-board\n")]
    assert "BTC" in board

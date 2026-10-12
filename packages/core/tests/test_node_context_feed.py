"""covers: spec 0009 AC-3 (subscribe, no credentials), AC-4 (nothing raised leaves the task), AC-5 (a
resubscribe snapshot is not published twice), AC-6 (backoff, idle watchdog, down and up logs, one
outage line from the last message before to the first after, a failed append only logged).

Drives ``ContextFeed`` against a local fake websocket server; no Nautilus node needed.
"""

import asyncio
import contextlib
import json
import logging
import time
from decimal import Decimal

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.server import serve

from trading_core.marketdata.context_feed import ForcedTrade, parse_outages
from trading_core.node.context_feed import ContextFeed

INFO = json.dumps({"event": "info", "version": 1})


def ticker(oi="2423.1378", ms=1791758735536):
    return f'{{"feed":"ticker","product_id":"PF_XBTUSD","time":{ms},"openInterest":{oi}}}'


def snapshot(*uids):
    rows = [
        {
            "product_id": "PF_XBTUSD",
            "feed": "trade",
            "uid": u,
            "side": "sell",
            "type": "liquidation",
            "time": 1791758800123,
            "qty": 0.5,
            "price": 83512.0,
        }
        for u in uids
    ]
    return json.dumps({"feed": "trade_snapshot", "product_id": "PF_XBTUSD", "trades": rows})


class Server:
    """A fake Kraken: each connection plays the next script, a list of messages then ``close``,
    ``hang`` (stay open, silent), or nothing more."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.received: list[list[str]] = []
        self.port = 0

    async def handler(self, ws):
        script = self.scripts.pop(0) if self.scripts else ["hang"]
        got: list[str] = []
        self.received.append(got)
        for _ in range(2):
            got.append(await ws.recv())
        for step in script:
            if step == "close":
                await ws.close()
                return
            if step == "hang":
                break
            await ws.send(step)
        # Stay open and silent until the client leaves (a bare Future would block the server's shutdown).
        await ws.wait_closed()


async def _run(server, until, *, idle_s=5.0, emit=None, outages_path=None, url=None, max_waits=None):
    """Run a feed against ``server`` until ``until(feed, emitted, waits)`` holds; returns them."""
    emitted = []
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if max_waits is not None and len(waits) >= max_waits:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    def default_emit(record, ts_init):
        emitted.append((record, ts_init))
        return None

    async with serve(server.handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        feed = ContextFeed(
            ws_url=url or f"ws://127.0.0.1:{port}",
            product_ids=["PF_XBTUSD"],
            reconnect_max_s=60,
            idle_s=idle_s,
            clock_ns=time.time_ns,
            emit=emit or default_emit,
            outages_path=outages_path,
            sleep=sleep,
        )
        task = asyncio.create_task(feed.run())
        deadline = time.monotonic() + 10
        while not until(feed, emitted, waits) and not task.done():
            assert time.monotonic() < deadline, "timed out"
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return feed, emitted, waits


def _codes(caplog, code):
    return [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == code]


def test_a_drop_reconnects_resubscribes_and_writes_one_outage(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    server = Server([[INFO, ticker(), ticker("2423.1379"), "close"], [INFO, ticker("2423.2")]])
    path = tmp_path / "run" / "context_outages.jsonl"

    feed, emitted, waits = asyncio.run(_run(server, lambda f, e, w: len(e) == 3, outages_path=lambda: path))

    assert [r.value for r, _ in emitted] == [Decimal("2423.1378"), Decimal("2423.1379"), Decimal("2423.2")]
    # Both connections subscribed to both feeds, with nothing but the subscribe messages.
    assert [[json.loads(m) for m in got] for got in server.received] == [
        [{"event": "subscribe", "feed": f, "product_ids": ["PF_XBTUSD"]} for f in ("ticker", "trade")]
    ] * 2
    assert waits == [1]
    (outage,) = feed.outages
    assert outage.reason == "closed"
    assert outage.start_ns == emitted[1][1]  # the last message before the drop
    assert outage.start_ns < outage.end_ns <= emitted[2][1]  # the first after it (the info event)
    assert parse_outages(path.read_text().splitlines()) == ([outage], 0)
    assert [f["reason"] for f in _codes(caplog, "context_feed_down")] == ["closed"]
    (up,) = _codes(caplog, "context_feed_up")
    assert up["outage_s"] >= 0


def test_a_silent_connection_counts_as_an_outage(caplog):
    caplog.set_level(logging.INFO)
    server = Server([[INFO, ticker(), "hang"], [INFO, ticker("1")]])

    feed, _, _ = asyncio.run(_run(server, lambda f, e, w: len(e) == 2, idle_s=0.3))

    assert [o.reason for o in feed.outages] == ["idle"]
    assert [f["reason"] for f in _codes(caplog, "context_feed_down")] == ["idle"]


def test_refused_connects_back_off_doubling_and_log_down_once(caplog):
    caplog.set_level(logging.INFO)

    feed, _, waits = asyncio.run(_run(Server([]), lambda f, e, w: False, url="ws://127.0.0.1:9", max_waits=8))

    assert waits == [1, 2, 4, 8, 16, 32, 60, 60]
    assert len(_codes(caplog, "context_feed_down")) == 1
    assert feed.outages == []  # no message ever came, so no outage has a start


def test_short_sessions_keep_doubling_the_backoff():
    server = Server([[INFO, ticker(), "close"]] * 3)

    _, _, waits = asyncio.run(_run(server, lambda f, e, w: False, max_waits=3))

    assert waits == [1, 2, 4]


def test_a_session_connected_past_the_reset_starts_the_backoff_from_one(monkeypatch):
    from trading_core.node import context_feed

    monkeypatch.setattr(context_feed, "BACKOFF_RESET_S", 0)  # every session counts as long enough
    server = Server([[INFO, ticker(), "close"]] * 3)

    _, _, waits = asyncio.run(_run(server, lambda f, e, w: False, max_waits=3))

    assert waits == [1, 1, 1]


def test_a_resubscribe_snapshot_publishes_no_uid_twice():
    server = Server([[INFO, snapshot("a", "b"), "close"], [INFO, snapshot("a", "b", "c")]])

    _, emitted, _ = asyncio.run(_run(server, lambda f, e, w: len(e) == 3))

    assert [r.trade_id for r, _ in emitted if isinstance(r, ForcedTrade)] == ["a", "b", "c"]


def test_a_failing_publish_never_ends_the_feed(caplog):
    caplog.set_level(logging.INFO)
    calls = []

    def emit(record, ts_init):
        calls.append(record)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return "instrument_not_loaded" if len(calls) == 2 else None

    server = Server([[INFO, ticker(), ticker(), ticker(), "{bad", ticker()]])
    asyncio.run(_run(server, lambda f, e, w: len(calls) == 4, emit=emit))

    reasons = [f["reason"] for f in _codes(caplog, "context_message_bad")]
    assert reasons == ["handler_error", "instrument_not_loaded", "bad_json"]


def test_a_failed_outage_append_is_only_logged(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    blocker = tmp_path / "file"
    blocker.write_text("")
    server = Server([[INFO, ticker(), "close"], [INFO, ticker()]])

    feed, _, _ = asyncio.run(
        _run(server, lambda f, e, w: len(e) == 2, outages_path=lambda: blocker / "x" / "context_outages.jsonl")
    )

    assert len(feed.outages) == 1
    assert len(_codes(caplog, "context_outage_write_failed")) == 1
    assert feed.state == "subscribed"

"""covers: spec 0003 AC-8 (the startup key check rules and watcher)."""

import asyncio
import json

import pytest

from trading_core.logs import configure_logging
from trading_core.node.key_check import key_check_state, watch_key_check


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize(
    ("confirmed", "elapsed", "expected"),
    [(True, 0, "confirmed"), (True, 99, "confirmed"), (False, 1, "waiting"), (False, 2, "failed")],
)
def test_key_check_state(confirmed, elapsed, expected):
    assert key_check_state(confirmed, elapsed, 2.0) == expected


class _FakeTime:
    """A monotonic clock the watcher's sleeps move forward, so no real time passes."""

    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _watch(confirmed_after, timeout_s=3.0):
    clock = _FakeTime()
    stops = []
    polls = []

    def is_confirmed():
        polls.append(clock.t)
        return confirmed_after is not None and clock.t - 100.0 >= confirmed_after

    async def run():
        task = asyncio.create_task(
            watch_key_check(is_confirmed, lambda: stops.append(True), timeout_s, poll_s=0, now=clock)
        )
        while not task.done():
            clock.t += 1.0
            await asyncio.sleep(0)
        return task.result()

    return asyncio.run(run()), stops, polls


def test_watcher_fails_closed_after_the_timeout_and_stops_the_node(capsys):
    configure_logging(process="trader", env="live")

    state, stops, _ = _watch(confirmed_after=None)

    assert (state, stops) == ("failed", [True])
    (line,) = [line for line in _lines(capsys) if line.get("code") == "kraken_auth_failed"]
    assert line["level"] == "CRITICAL"


def test_watcher_confirms_once_the_account_appears_and_never_stops_the_node(capsys):
    configure_logging(process="trader", env="live")

    state, stops, _ = _watch(confirmed_after=2.0)

    assert (state, stops) == ("confirmed", [])
    assert "kraken account confirmed" in [line["msg"] for line in _lines(capsys)]

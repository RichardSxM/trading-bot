"""covers: spec 0013 AC-2 (the ping, only while the Redis read is fresh) and AC-14 (``/fail`` while pushes fail)."""

import asyncio

import httpx
from pydantic import SecretStr

from alerter.watchdog import Watchdog

URL = SecretStr("https://hc-ping.com/0f3c3a0e-5d8c-4d0e-9f7c-123456789abc")


def _ping(statuses, *, failing, sleeps=None, timeouts=None):
    seen: list[str] = []
    answers = list(statuses)

    def answer(request):
        seen.append(str(request.url))
        if timeouts is not None:
            timeouts.append(request.extensions["timeout"])
        status = answers.pop(0)
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status)

    async def sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await Watchdog(http, URL, sleep=sleep).ping(failing=failing)

    return asyncio.run(run()), seen


def test_a_healthy_alerter_pings_the_plain_url():
    ok, seen = _ping([200], failing=False)

    assert ok
    assert seen == [URL.get_secret_value()]


def test_a_failing_push_path_pings_fail():
    ok, seen = _ping([200], failing=True)

    assert ok
    assert seen == [URL.get_secret_value() + "/fail"]


def test_a_ping_retries_three_times_then_gives_up_quietly():
    ok, seen = _ping([500, httpx.ConnectError("down"), 503, 500], failing=False)

    assert not ok
    assert len(seen) == 4


def test_retries_wait_1_2_and_4_s_and_the_give_up_is_logged(caplog):
    sleeps: list[float] = []

    ok, _ = _ping([500, 500, 500, 500], failing=False, sleeps=sleeps)

    assert not ok
    assert sleeps == [1.0, 2.0, 4.0]
    assert [r.fields["code"] for r in caplog.records if hasattr(r, "fields")] == ["watchdog_ping_failed"]


def test_each_ping_times_out_after_10_s():
    timeouts: list[dict[str, float]] = []

    _ping([200], failing=False, timeouts=timeouts)

    assert timeouts == [{"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}]

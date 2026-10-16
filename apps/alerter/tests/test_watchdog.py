"""covers: spec 0013 AC-2 (the ping, only while the Redis read is fresh) and AC-14 (``/fail`` while pushes fail)."""

import asyncio

import httpx
from pydantic import SecretStr

from alerter.watchdog import Watchdog

URL = SecretStr("https://hc-ping.com/0f3c3a0e-5d8c-4d0e-9f7c-123456789abc")


def _ping(statuses, *, failing):
    seen: list[str] = []
    answers = list(statuses)

    def answer(request):
        seen.append(str(request.url))
        status = answers.pop(0)
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status)

    async def sleep(seconds):
        pass

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

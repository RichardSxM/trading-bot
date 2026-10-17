"""covers: spec 0013 *Pushover fields* (priority per tier, critical retry and expiry, cut texts) and AC-14
(retries at 5, 15, 30, 60 s on network errors, 5xx, and 429; no retry on another 4xx)."""

import asyncio

import httpx
import pytest
from pydantic import SecretStr

from alerter.delivery import LogDelivery, PushoverDelivery
from alerter.pushover import MESSAGES_URL, PushoverClient, fields
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Tier

from .conftest import NOW

TOKEN = SecretStr("azGDORePK8gMaC0QOYAMyEEuzJnyUi")
USER = SecretStr("uQiRzpo4DXghDmr9QzzfQu27cmVRsG")


def _push(tier: Tier):
    return AlertEngine(EngineConfig(env="demo")).test_episodes("cmd", [tier], NOW).pushes[0]


class Recorder:
    """An httpx transport answering from a list of statuses (or exceptions), recording every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        body = {"status": 1, "request": "r1"} if answer == 200 else {"status": 0, "errors": ["nope"]}
        return httpx.Response(answer, json=body)


def _send(recorder, push):
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
            return await PushoverClient(http, TOKEN, USER, sleep=sleep).send(push)

    return asyncio.run(run()), sleeps


@pytest.mark.parametrize(("tier", "priority"), [(Tier.CRITICAL, "2"), (Tier.WARNING, "0"), (Tier.INFO, "-1")])
def test_fields_per_tier(tier, priority):
    values = fields(_push(tier), TOKEN, USER)

    assert values["priority"] == priority
    assert values["token"] == TOKEN.get_secret_value()
    assert values["title"].startswith("[demo] TEST")
    assert values["timestamp"] == str(int(NOW.timestamp()))
    critical = {"retry": "60", "expire": "3600", "sound": "persistent"}
    assert (critical.items() <= values.items()) is (tier is Tier.CRITICAL)


def test_a_push_is_posted_once_when_pushover_accepts_it():
    recorder = Recorder(200)

    result, sleeps = _send(recorder, _push(Tier.WARNING))

    assert (result.ok, result.attempts, sleeps) == (True, 1, [])
    assert str(recorder.requests[0].url) == MESSAGES_URL


def test_5xx_429_and_network_errors_retry_at_5_15_30_60_then_fail():
    recorder = Recorder(503, 429, httpx.ConnectTimeout("slow"), 502, 500)

    result, sleeps = _send(recorder, _push(Tier.CRITICAL))

    assert (result.ok, result.status, result.attempts) == (False, 500, 5)
    assert sleeps == [5.0, 15.0, 30.0, 60.0]


def test_a_retry_that_succeeds_stops_retrying():
    result, sleeps = _send(Recorder(503, 200), _push(Tier.INFO))

    assert (result.ok, result.attempts, sleeps) == (True, 2, [5.0])


def test_a_bad_token_is_not_retried():
    result, sleeps = _send(Recorder(400), _push(Tier.CRITICAL))

    assert (result.ok, result.status, result.attempts, sleeps) == (False, 400, 1, [])


def test_a_200_without_status_1_is_a_failure():
    def answer(request):
        return httpx.Response(200, json={"status": 0})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await PushoverClient(http, TOKEN, USER, delays=()).send(_push(Tier.INFO))

    assert not asyncio.run(run()).ok


def test_an_accepted_push_with_no_active_devices_fails_without_a_quick_retry():
    def answer(request):
        return httpx.Response(200, json={"status": 1, "request": "r1", "info": "no active devices to send to"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            return await PushoverDelivery(PushoverClient(http, TOKEN, USER)).send(_push(Tier.CRITICAL))

    outcome = asyncio.run(run())

    # The engine's retry cycle sends it again once a device is logged in.
    assert outcome.delivery is Delivery.FAILED


def test_pushover_delivery_maps_results_to_sent_or_failed():
    async def run(status):
        async with httpx.AsyncClient(transport=httpx.MockTransport(Recorder(status))) as http:
            return await PushoverDelivery(PushoverClient(http, TOKEN, USER, delays=())).send(_push(Tier.INFO))

    assert asyncio.run(run(200)).delivery is Delivery.SENT
    assert asyncio.run(run(401)).delivery is Delivery.FAILED


def test_log_delivery_logs_the_push_and_sends_nothing(caplog):
    caplog.set_level("INFO")

    outcome = asyncio.run(LogDelivery().send(_push(Tier.CRITICAL)))

    assert outcome.delivery is Delivery.DRY_RUN
    [record] = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "alert_dry_run"]
    assert record.fields["tier"] == "critical"


def test_each_pushover_call_times_out_after_10_s():
    # covers: AC-14, "timeout of 10 s"
    recorder = Recorder(200)

    _send(recorder, _push(Tier.INFO))

    assert recorder.requests[0].extensions["timeout"] == {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}

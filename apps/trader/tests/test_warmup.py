"""covers: spec 0025 AC-7 (the fetch window and a good seed), AC-9 (retries, then an empty seed and one
``warmup_seed_failed`` warning the alert rule catches; it never raises), AC-20 (one fetch per coin from that
coin's candles, the coin on the warning, and a failed coin leaving the others seeded).

The fetch is scripted: no request leaves the test.
"""

import json
import logging
import urllib.error
from datetime import UTC, datetime
from email.message import Message

import pytest

from trader.warmup import SEED_BARS, warmup_seed, warmup_seeds
from trading_core.alerts.model import Observation
from trading_core.alerts.rules import first_match

INTERVAL_MS = 300_000
NOW_S = int(datetime(2026, 10, 30, 12, 2, 30, tzinfo=UTC).timestamp())
NOW_NS = NOW_S * 1_000_000_000
BTC = "PF_XBTUSD.KRAKEN"
PEPE = "PF_PEPEUSD.KRAKEN"
BTC_URL = "https://futures.kraken.com/api/charts/v1/trade/PF_XBTUSD/5m"


def _body(count=620, more=False):
    # The last candle opens at 12:00 and is still in progress at 12:02:30.
    last_open = (NOW_S - 150) * 1000
    candles = [
        {
            "time": last_open - (count - 1 - i) * INTERVAL_MS,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "2",
        }
        for i in range(count)
    ]
    return json.dumps({"candles": candles, "more_candles": more}).encode()


class _Script:
    """A scripted ``get``: each call takes the next outcome, an exception to raise or bytes to return."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.urls = []
        self.waits = []

    def get(self, url, timeout_s):
        self.urls.append((url, timeout_s))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sleep(self, seconds):
        self.waits.append(seconds)


def _seed(script, coin=BTC):
    return warmup_seed(coin, lambda: NOW_NS, script.get, script.sleep)


def _http(code):
    return urllib.error.HTTPError(BTC_URL, code, "x", Message(), None)


def _failures(caplog):
    return [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "warmup_seed_failed"]


def test_a_good_fetch_gives_the_latest_600_complete_candles_from_one_request():
    script = _Script(_body())

    seed = _seed(script)

    assert len(seed) == SEED_BARS
    assert seed[-1].ts_ns == (NOW_S - 150) * 1_000_000_000  # the 11:55 candle, closed at 12:00
    [(url, timeout)] = script.urls
    assert url == f"{BTC_URL}?from={NOW_S - 620 * 300}&to={NOW_S}"
    assert timeout == 5.0


@pytest.mark.parametrize("error", [TimeoutError("slow"), urllib.error.URLError("refused"), _http(429), _http(503)])
def test_a_passing_problem_is_retried_after_1_then_2_seconds(error):
    script = _Script(error, error, _body())

    assert len(_seed(script)) == SEED_BARS
    assert script.waits == [1.0, 2.0]


def test_a_fetch_that_times_out_every_try_starts_without_a_seed_and_warns_once(caplog):
    script = _Script(TimeoutError("slow"), TimeoutError("slow"), TimeoutError("slow"))

    with caplog.at_level(logging.WARNING):
        assert _seed(script) == ()

    assert len(script.urls) == 3
    [warning] = _failures(caplog)
    assert warning.levelname == "WARNING"
    assert warning.fields["reason"] == "fetch_failed"
    assert warning.fields["instrument"] == BTC
    obs = Observation(
        source="log:trader",
        service="trader",
        at=datetime(2026, 10, 30, tzinfo=UTC),
        code="warmup_seed_failed",
        level="WARNING",
        fields=dict(warning.fields),
    )
    rule = first_match(obs)
    assert rule is not None
    assert rule.id == "warmup_seed_failed"


def test_an_error_not_worth_retrying_fails_at_once(caplog):
    script = _Script(_http(404))

    with caplog.at_level(logging.WARNING):
        assert _seed(script) == ()

    assert (len(script.urls), script.waits) == (1, [])
    assert [r.fields["reason"] for r in _failures(caplog)] == ["fetch_failed"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"<html>busy</html>", "not_json"),
        (b"\xff\xfe", "not_json"),
        (_body(more=True), "more_candles"),
        (_body(count=150), "too_few"),
    ],
)
def test_a_refused_seed_is_empty_with_its_reason(body, reason, caplog):
    with caplog.at_level(logging.WARNING):
        assert _seed(_Script(body)) == ()

    assert [r.fields["reason"] for r in _failures(caplog)] == [reason]


def test_an_unexpected_error_never_leaves_startup(caplog):
    def broken(url, timeout_s):
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        assert warmup_seed(BTC, lambda: NOW_NS, broken, lambda s: None) == ()

    assert [r.fields["reason"] for r in _failures(caplog)] == ["fetch_failed"]


def test_a_coin_fetches_its_own_candles():
    script = _Script(_body())

    assert len(_seed(script, PEPE)) == SEED_BARS
    [(url, _)] = script.urls
    assert url == f"https://futures.kraken.com/api/charts/v1/trade/PF_PEPEUSD/5m?from={NOW_S - 620 * 300}&to={NOW_S}"


def test_each_coin_is_fetched_in_turn_and_a_failed_coin_alone_starts_without_a_seed(caplog):
    # PEPE (first in the list) is refused, BTC still fetches after it.
    script = _Script(b"<html>busy</html>", _body())

    with caplog.at_level(logging.WARNING):
        seeds = warmup_seeds([PEPE, BTC], lambda coin: _seed(script, coin))

    assert [url.split("/")[-2] for url, _ in script.urls] == ["PF_PEPEUSD", "PF_XBTUSD"]
    assert seeds[PEPE] == ()
    assert len(seeds[BTC]) == SEED_BARS
    [warning] = _failures(caplog)
    assert (warning.fields["instrument"], warning.fields["reason"]) == (PEPE, "not_json")

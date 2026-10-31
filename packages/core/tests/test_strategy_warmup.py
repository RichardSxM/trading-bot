"""covers: spec 0025 AC-7 (the latest 600 complete candles as bar closes) and AC-8 (every seed refusal)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_core.strategy.warmup import Candle, SeedRefusedError, parse_candles, seed_bars

D = Decimal
INTERVAL_MS = 300_000
INTERVAL_NS = INTERVAL_MS * 1_000_000
T0_MS = int(datetime(2026, 10, 30, tzinfo=UTC).timestamp()) * 1000


def _raw(i, *, open_="100", high="101", low="99", close="100", volume="1.5", time_ms=None):
    return {
        "time": T0_MS + i * INTERVAL_MS if time_ms is None else time_ms,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _payload(count, more=False, **kwargs):
    return {"candles": [_raw(i, **kwargs) for i in range(count)], "more_candles": more}


def _now_after(count):
    """The wall clock right as the ``count``-th candle (0 based ``count - 1``) closes."""
    return (T0_MS + count * INTERVAL_MS) * 1_000_000


def _bars(payload, now_ns, keep=600, minimum=200):
    return seed_bars(parse_candles(payload), INTERVAL_NS, now_ns, keep, minimum)


def test_600_good_candles_become_bar_closes_oldest_first_stamped_with_their_close_time():
    bars = _bars(_payload(600), _now_after(600))

    assert len(bars) == 600
    assert bars[0].ts_ns == (T0_MS + INTERVAL_MS) * 1_000_000
    assert bars[-1].ts_ns == _now_after(600)
    assert (bars[0].open, bars[0].high, bars[0].low, bars[0].close, bars[0].volume) == (
        D(100),
        D(101),
        D(99),
        D(100),
        D("1.5"),
    )


def test_a_feed_of_650_keeps_the_latest_600():
    bars = _bars(_payload(650), _now_after(650))

    assert len(bars) == 600
    assert bars[0].ts_ns == (T0_MS + 51 * INTERVAL_MS) * 1_000_000


def test_a_candle_still_in_progress_is_dropped():
    # 620 sent, the clock 1 ms before the last one closes.
    bars = _bars(_payload(620), _now_after(620) - 1_000_000)

    assert len(bars) == 600
    assert bars[-1].ts_ns == _now_after(619)


def test_a_missing_candle_inside_the_seed_is_allowed():
    payload = _payload(300)
    del payload["candles"][150]

    assert len(_bars(payload, _now_after(300))) == 299


def test_exactly_200_complete_candles_is_enough_and_199_is_too_few():
    assert len(_bars(_payload(200), _now_after(200))) == 200
    with pytest.raises(SeedRefusedError) as refused:
        _bars(_payload(200), _now_after(200) - 1)
    assert refused.value.reason == "too_few"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ([], "bad_payload"),
        ({"result": "ok"}, "bad_payload"),
        ({"candles": "none"}, "bad_payload"),
        ({"candles": [1]}, "bad_payload"),
        ({"candles": [], "more_candles": "no"}, "bad_payload"),
        (
            {"candles": [{"time": "1", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"}]},
            "bad_payload",
        ),
        ({"candles": [], "more_candles": True}, "more_candles"),
        ({"candles": [_raw(0, close="abc")]}, "bad_decimal"),
        ({"candles": [_raw(0, high=101)]}, "bad_decimal"),
        ({"candles": [_raw(0, low="NaN")]}, "bad_decimal"),
        ({"candles": [_raw(0, volume=None)]}, "bad_decimal"),
    ],
)
def test_a_payload_of_the_wrong_shape_is_refused(payload, reason):
    with pytest.raises(SeedRefusedError) as refused:
        parse_candles(payload)
    assert refused.value.reason == reason


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"high": "98"}, "bad_range"),  # high below low
        ({"close": "102"}, "bad_range"),  # close above the high
        ({"close": "98"}, "bad_range"),  # close below the low
        ({"time_ms": T0_MS + 5 * INTERVAL_MS + 1000}, "off_boundary"),
    ],
)
def test_one_bad_candle_refuses_the_whole_seed(bad, reason):
    payload = _payload(300)
    payload["candles"][6] = _raw(6, **bad)

    with pytest.raises(SeedRefusedError) as refused:
        _bars(payload, _now_after(300))
    assert refused.value.reason == reason


def test_open_times_that_repeat_or_go_back_are_refused():
    for wrong in (T0_MS + 4 * INTERVAL_MS, T0_MS + 3 * INTERVAL_MS):
        payload = _payload(300)
        payload["candles"][5] = _raw(5, time_ms=wrong)
        with pytest.raises(SeedRefusedError) as refused:
            _bars(payload, _now_after(300))
        assert refused.value.reason == "not_increasing"


def test_a_close_on_the_high_or_low_is_in_range():
    candles = [
        Candle(T0_MS + i * INTERVAL_MS, D(100), D(101), D(99), D(101 if i % 2 else 99), D(1)) for i in range(200)
    ]

    assert len(seed_bars(candles, INTERVAL_NS, _now_after(200), 600, 200)) == 200

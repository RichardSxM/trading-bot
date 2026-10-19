"""covers: spec 0015 AC-2 (the packaged thresholds parse, round trip through JSON, and refuse bad values)."""

from datetime import date
from decimal import Decimal

import pytest

from trading_core.gate.thresholds import Thresholds, ThresholdsError, parse_thresholds
from trading_core.versioning import package_root

PACKAGED = (package_root() / "gate" / "thresholds.toml").read_bytes()


def test_the_packaged_file_holds_the_v1_values():
    t = parse_thresholds(PACKAGED)

    assert t.version == 1
    assert t.holdout_start == date(2025, 10, 19)
    assert t.fee_hurdle_k == Decimal("1.5")
    assert t.max_drawdown == Decimal("0.10")
    assert (t.candle.min_holdout_trades, t.replay.min_holdout_trades) == (60, 200)
    assert (t.candle.first_in_sample_months, t.candle.step_months) == (12, 6)
    assert (t.replay.first_in_sample_days, t.replay.step_days) == (15, 7)
    assert t.paper.slippage_tolerance_bps == Decimal(2)
    assert t.bootstrap_resamples == 10_000


def test_json_round_trip_gives_the_same_thresholds():
    t = parse_thresholds(PACKAGED)
    stored = t.to_json()

    assert stored["holdout_start"] == "2025-10-19"
    assert stored["max_drawdown"] == "0.10"
    assert Thresholds.from_json(stored) == t


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b"version = 1", b"version = 2", "version 2"),
        (b"max_drawdown = 0.10", b"max_drawdown = 1.5", "max_drawdown"),
        (b"cscv_blocks = 10", b"cscv_blocks = 9", "cscv_blocks"),
        (b"pbo_max = 0.30\n", b"", "missing ['pbo_max']"),
        (b"version = 1", b"version = 1\nsurprise = 3", "unknown ['surprise']"),
        (b"step_days = 7", b"step_days = 0", "replay.step_days"),
        (b"holdout_start = 2025-10-19", b'holdout_start = "soon"', "ISO date"),
    ],
)
def test_a_bad_value_is_refused(old, new, message):
    with pytest.raises(ThresholdsError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_thresholds(PACKAGED.replace(old, new))


def test_broken_toml_is_refused():
    with pytest.raises(ThresholdsError, match="not valid TOML"):
        parse_thresholds(b"version = = 1")

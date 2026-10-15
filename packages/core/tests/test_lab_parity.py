"""covers: spec 0008 AC-15 (the parity check: the [paper] config and its hash, which hours count,
excluded boundaries, and the matching)."""

from decimal import Decimal

import pytest

from trading_core.lab.parity import (
    MATCH_NS,
    PAPER_KEYS,
    Decided,
    PaperRun,
    ParityInputError,
    Window,
    compare,
    config_sha256,
    counted_windows,
    differing_keys,
    eligible_hours,
    paper_config,
)
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.rows import JsonObject
from trading_core.settings import PaperLoopSettings

MIN = 60_000_000_000
T0 = 1_791_446_400_000_000_000  # 2026-10-08 08:00 UTC
DELAY = 2_000_000_000  # decisions come 2 s after the boundary

PAPER: JsonObject = {
    "placeholder_interval_min": 15,
    "placeholder_hold_min": 10,
    "placeholder_lookback_min": 5,
    "placeholder_stop_pct": "0.3",
    "risk_max_order_notional_usd": "250",
    "risk_max_leverage": "2",
    "risk_stale_quote_s": "10",
    "risk_per_trade": "0.005",
    "risk_max_total_exposure_usd": "500",
    "risk_maintenance_margin_rate": "0.01",
    "risk_min_liq_distance_pct": "10",
    "risk_liq_stop_multiple": "3",
    "risk_stop_slippage_bps": "5",
}


def _enter(minute: int, side: str = "buy", *, approved: bool = True, reason: str | None = None, jitter: int = 0):
    return Decided(T0 + minute * MIN + DELAY + jitter, "enter", side, approved, reason)


def _beats(minutes, **kwargs):
    return [_enter(m, **kwargs) for m in minutes]


WHOLE = [Window(T0, T0 + 4 * HOUR_NS)]


def test_the_paper_keys_are_exactly_the_traders_strategy_config_keys():
    fields = {name for name in PaperLoopSettings.model_fields if name.startswith(("placeholder_", "risk_"))}

    assert set(PAPER_KEYS) == fields


def test_the_paper_table_reads_as_the_paper_loops_settings():
    strategy = paper_config(PAPER)

    assert (strategy.interval_min, strategy.hold_min, strategy.lookback_min) == (15, 10, 5)
    assert strategy.stop_pct == Decimal("0.3")
    assert (strategy.risk.max_order_notional, strategy.risk.max_leverage) == (Decimal(250), Decimal(2))
    assert (strategy.risk.max_total_exposure, strategy.risk.stop_slippage_bps) == (Decimal(500), Decimal(5))
    assert strategy.warmup_ns == 15 * MIN


def test_every_bad_paper_key_is_listed_at_once():
    table = {**PAPER, "placeholder_stop_pct": 0.3, "turbo": True, "placeholder_hold_min": "10"}
    del table["risk_max_leverage"]

    with pytest.raises(ParityInputError) as raised:
        paper_config(table)

    problems = "\n".join(raised.value.problems)
    assert "turbo: not a paper setting" in problems
    assert "risk_max_leverage: missing" in problems
    assert "placeholder_stop_pct: must be a string" in problems
    assert "placeholder_hold_min: must be a whole number" in problems


def test_the_paper_loops_timing_rules_hold():
    with pytest.raises(ParityInputError, match="must be below placeholder_interval_min"):
        paper_config({**PAPER, "placeholder_hold_min": 15})
    with pytest.raises(ParityInputError, match="must divide 60"):
        paper_config({**PAPER, "placeholder_interval_min": 7, "placeholder_hold_min": 5})


def test_the_hash_is_canonical_and_names_the_differing_keys():
    reordered = dict(reversed(list(PAPER.items())))

    assert config_sha256(reordered) == config_sha256(PAPER)
    assert config_sha256({**PAPER, "placeholder_stop_pct": "0.30"}) != config_sha256(PAPER)
    changed = {**PAPER, "placeholder_stop_pct": "0.5", "extra": 1}
    assert differing_keys(changed, PAPER) == ["extra", "placeholder_stop_pct"]


def test_only_hours_wholly_inside_a_matching_run_are_eligible():
    good = config_sha256(PAPER)
    runs = [
        PaperRun("a", T0 + 30 * MIN, T0 + 3 * HOUR_NS, PAPER, good),  # 08:30 to 11:00
        PaperRun("b", T0 + 3 * HOUR_NS, T0 + 6 * HOUR_NS, {}, "other"),  # another config
    ]
    hours = [T0 + n * HOUR_NS for n in range(6)]

    # 08:00 starts before run a; 09:00 and 10:00 fit; 11:00 onwards belong to run b.
    assert eligible_hours(hours, runs, good) == [T0 + HOUR_NS, T0 + 2 * HOUR_NS]


def test_the_warm_up_is_cut_from_each_segment():
    windows = counted_windows([(T0, T0 + HOUR_NS - 1), (T0 + 2 * HOUR_NS, T0 + 2 * HOUR_NS + 10 * MIN)], 15 * MIN)

    # The second segment is shorter than the warm up, so it has no counted window.
    assert windows == [Window(T0 + 15 * MIN, T0 + HOUR_NS - 1)]


def test_a_replay_of_the_paper_decisions_scores_one():
    paper = _beats(range(15, 240, 15))
    replay = [_enter(m, jitter=3_000_000) for m in range(15, 240, 15)]

    result = compare(paper, replay, WHOLE)

    assert result.ratio == 1
    assert result.passed
    assert (result.unmatched_paper, result.unmatched_replay) == ((), ())


def test_removing_a_tenth_of_the_paper_decisions_fails():
    replay = _beats(range(0, 300 * 15, 15))
    paper = [d for n, d in enumerate(replay) if n % 10 != 0]
    windows = [Window(T0, T0 + 300 * 15 * MIN)]

    result = compare(paper, replay, windows)

    assert result.matches == 270
    assert result.ratio == Decimal("0.9")
    assert not result.passed
    assert len(result.unmatched_replay) == 30


def test_a_blocked_paper_refusal_is_left_out_with_its_replay_twin():
    paper = _beats(range(15, 240, 15))
    paper[3] = _enter(60, approved=False, reason="blocked:stale_feed")
    paper[5] = _enter(90, approved=False, reason="paused")
    replay = _beats(range(15, 240, 15))

    result = compare(paper, replay, WHOLE)

    assert result.ratio == 1
    assert (result.paper, result.replay) == (13, 13)
    assert [d.decided_ns for d in result.excluded] == [paper[3].decided_ns, paper[5].decided_ns]


def test_a_risk_refusal_still_counts_and_matches_an_approval():
    paper = [_enter(15, approved=False, reason="max_leverage")]
    replay = [_enter(15)]

    assert compare(paper, replay, WHOLE).ratio == 1


def test_side_and_time_decide_a_match():
    paper = [_enter(15, "buy"), _enter(30, "sell")]
    replay = [_enter(15, "sell"), _enter(30, "sell", jitter=MATCH_NS + 1)]

    result = compare(paper, replay, WHOLE)

    assert result.matches == 0
    assert len(result.unmatched_paper) == 2


def test_decisions_outside_the_windows_and_exits_are_ignored():
    paper = [_enter(5), Decided(T0 + 20 * MIN, "exit", "sell", True)]
    replay = [_enter(5)]

    result = compare(paper, replay, [Window(T0 + 15 * MIN, T0 + HOUR_NS)])

    assert result.ratio is None
    assert not result.passed

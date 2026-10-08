"""covers: spec 0007 AC-4 (the three gap kinds and their dedupe), AC-5 (gap seconds clipped to a day)."""

import pytest

from trading_core.marketdata.gaps import Gap, GapDetector, GapState, gap_kind, seconds_within
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS

T = 1_791_439_200 * SECOND_NS
S = SECOND_NS
SILENCE = 10 * S
BTC = "PF_XBTUSD.KRAKEN"


def _detector(state=None):
    return GapDetector(BTC, state or GapState(), silence_ns=SILENCE)


def test_steady_rows_give_no_gap():
    detector = _detector()
    ts = [T + i * S for i in range(30)]
    assert detector.scan("a", ts, [i == 0 for i in range(30)]) == []
    assert detector.state == GapState(last_ts=ts[-1], run="a", cleared=True)


def test_silence_at_the_threshold_and_not_below():
    detector = _detector()
    gaps = detector.scan("a", [T, T + 9 * S, T + 21 * S], [True, False, False])
    assert gaps == [Gap(T + 9 * S, BTC, "silence", T + 21 * S)]
    assert _detector().scan("a", [T, T + 10 * S], [True, False]) == [Gap(T, BTC, "silence", T + 10 * S)]


def test_a_later_clear_is_a_reconnect_and_not_also_a_silence():
    detector = _detector()
    gaps = detector.scan("a", [T, T + S, T + 40 * S, T + 41 * S], [True, False, True, False])
    assert gaps == [Gap(T + S, BTC, "reconnect", T + 40 * S)]
    assert gaps[0].duration_s == 39.0


def test_the_first_clear_of_a_run_is_not_a_reconnect():
    assert _detector().scan("a", [T, T + S, T + 2 * S], [False, True, False]) == []


def test_a_new_run_is_a_recorder_down_gap_and_its_first_clear_starts_fresh():
    detector = _detector()
    detector.scan("a", [T, T + S], [True, False])
    gaps = detector.scan("b", [T + 41 * S, T + 42 * S, T + 60 * S], [True, False, True])
    assert gaps == [
        Gap(T + S, BTC, "recorder_down", T + 41 * S),
        Gap(T + 42 * S, BTC, "reconnect", T + 60 * S),
    ]


def test_state_carries_across_chunks_and_passes():
    first = _detector()
    first.scan("a", [T, T + S], [True, False])
    # A new detector from the saved state, as the next pass builds it from done.json.
    again = _detector(GapState.from_dict(first.state.to_dict()))
    assert again.scan("a", [T + 30 * S], [False]) == [Gap(T + S, BTC, "silence", T + 30 * S)]
    assert again.scan("a", [T + 31 * S], [True]) == [Gap(T + 30 * S, BTC, "reconnect", T + 31 * S)]


def test_an_empty_chunk_changes_nothing():
    detector = _detector()
    detector.scan("a", [T], [True])
    assert detector.scan("b", [], []) == []
    assert detector.scan("b", [T + 5 * S], [True]) == [Gap(T, BTC, "recorder_down", T + 5 * S)]


def test_state_from_an_empty_dict_is_the_start():
    assert GapState.from_dict({}) == GapState()


def test_gap_kind_rejects_unknown_values():
    assert gap_kind("silence") == "silence"
    with pytest.raises(ValueError, match="unknown gap kind"):
        gap_kind("outage")


def test_seconds_within_clips_to_the_window():
    gap = Gap(T - 600 * S, BTC, "recorder_down", T + 1_200 * S)  # 23:50 to 00:20 around T
    assert seconds_within(gap, T - HOUR_NS, T) == 600.0
    assert seconds_within(gap, T, T + HOUR_NS) == 1_200.0
    assert seconds_within(gap, T + HOUR_NS, T + 2 * HOUR_NS) == 0.0

"""covers: spec 0007 AC-4 (the four gap kinds and their precedence), AC-5 (gap seconds clipped to a
day), AC-16 (the sequence gap and where the book is cut)."""

import pytest

from trading_core.marketdata.gaps import Gap, GapDetector, GapState, PendingSequence, gap_kind, seconds_within
from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.marketdata.sequence import NEVER_NS, UNKNOWN, RunHoles, SequenceHole

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


def _holes(*holes):
    return RunHoles(holes=tuple(sorted(holes, key=lambda h: h.before_ns)), checked_to_ns=T + HOUR_NS)


def _hole(before, after, missing=5):
    return SequenceHole(100, 100 + missing - 1, missing, before, after)


def test_a_hole_is_a_sequence_gap_up_to_the_first_clear_at_or_after_its_after_time():
    detector = _detector()
    holes = _holes(_hole(T + 2 * S, T + 3 * S))
    ts = [T, T + S, T + 2 * S, T + 3 * S, T + 20 * S, T + 21 * S, T + 22 * S, T + 40 * S]
    clear = [True, False, False, False, False, True, False, False]
    scan = detector.scan_book("a", ts, clear, holes)
    # The CLEAR at T + 21 s ends the gap and is not a reconnect; the 17 s pause inside the gap is
    # not a silence, while the 18 s one after it is.
    assert scan.gaps == [Gap(T + 2 * S, BTC, "sequence", T + 21 * S), Gap(T + 22 * S, BTC, "silence", T + 40 * S)]
    assert scan.gaps[0].missing == 5
    assert (scan.blocked, scan.cuts, scan.resumes) == (False, [3], [5])


def test_a_clear_before_the_after_time_does_not_end_the_gap():
    detector = _detector()
    holes = _holes(_hole(T + S, T + 30 * S))
    ts = [T, T + S, T + 2 * S, T + 3 * S, T + 31 * S]
    clear = [True, False, False, True, True]
    scan = detector.scan_book("a", ts, clear, holes)
    assert scan.gaps == [Gap(T + S, BTC, "sequence", T + 31 * S)]
    assert (scan.cuts, scan.resumes) == ([2], [4])


def test_rows_at_the_before_time_are_still_trusted():
    detector = _detector()
    holes = _holes(_hole(T + S, T + 2 * S))
    scan = detector.scan_book("a", [T, T + S, T + S, T + 2 * S], [True, False, False, False], holes)
    assert scan.cuts == [3]


def test_the_gap_stays_open_across_chunks_and_passes():
    first = _detector()
    holes = _holes(_hole(T + S, T + 50 * S))
    assert first.scan_book("a", [T, T + S, T + 2 * S], [True, False, False], holes).cuts == [2]
    assert first.state.sequence == PendingSequence(T + S, T + 50 * S, 5)
    again = _detector(GapState.from_dict(first.state.to_dict()))
    scan = again.scan_book("a", [T + 30 * S, T + 60 * S], [False, True], holes)
    assert scan.blocked
    assert scan.gaps == [Gap(T + S, BTC, "sequence", T + 60 * S)]
    assert again.state.sequence is None


def test_holes_inside_an_open_gap_extend_it_and_add_their_counts():
    detector = _detector()
    holes = _holes(_hole(T + S, T + 2 * S, missing=3), _hole(T + 3 * S, T + 9 * S, missing=4))
    ts = [T, T + 2 * S, T + 4 * S, T + 5 * S, T + 9 * S]
    scan = detector.scan_book("a", ts, [True, False, False, True, True], holes)
    assert scan.gaps == [Gap(T + S, BTC, "sequence", T + 9 * S)]
    assert scan.gaps[0].missing == 7
    assert scan.cuts == [1]


def test_without_a_clear_the_gap_ends_at_the_runs_last_row():
    detector = _detector()
    holes = _holes(_hole(T + S, T + 2 * S))
    detector.scan_book("a", [T, T + S, T + 2 * S, T + 3 * S], [True, False, False, False], holes)
    gaps = detector.scan("b", [T + 70 * S], [True])
    assert gaps == [
        Gap(T + S, BTC, "sequence", T + 3 * S),
        Gap(T + 3 * S, BTC, "recorder_down", T + 70 * S),
    ]


def test_end_run_closes_an_open_gap_at_the_last_row():
    detector = _detector()
    detector.scan_book("a", [T, T + 2 * S], [True, False], _holes(_hole(T + S, T + 9 * S)))
    assert detector.end_run() == [Gap(T + S, BTC, "sequence", T + 2 * S)]
    assert detector.end_run() == []


def test_an_instrument_quiet_after_the_hole_gets_no_sequence_gap():
    detector = _detector()
    scan = detector.scan_book("a", [T, T + S], [True, False], _holes(_hole(T + 5 * S, T + 6 * S)))
    assert scan.gaps == []
    assert scan.cuts == []
    assert detector.end_run() == []


def test_a_fail_safe_hole_over_the_whole_run_starts_at_its_first_row():
    detector = _detector()
    holes = RunHoles().with_fail_safe()
    scan = detector.scan_book("a", [T, T + S, T + 2 * S], [True, False, True], holes)
    assert scan.cuts == [0]
    assert scan.resumes == []
    gaps = detector.end_run()
    assert gaps == [Gap(T, BTC, "sequence", T + 2 * S)]
    assert gaps[0].missing == UNKNOWN
    assert holes.holes[0].after_ns == NEVER_NS


def test_a_hole_already_passed_is_not_read_again():
    detector = _detector()
    holes = _holes(_hole(T + S, T + 2 * S))
    detector.scan_book("a", [T, T + S, T + 2 * S, T + 3 * S], [True, False, False, True], holes)
    scan = detector.scan_book("a", [T + 4 * S, T + 5 * S], [False, False], holes)
    assert scan.gaps == []
    assert scan.cuts == []
    assert not scan.blocked


def test_a_cut_and_a_resume_can_share_a_row():
    detector = _detector()
    scan = detector.scan_book("a", [T, T + 3 * S], [True, True], _holes(_hole(T + S, T + 2 * S)))
    assert scan.gaps == [Gap(T + S, BTC, "sequence", T + 3 * S)]
    assert (scan.cuts, scan.resumes) == ([1], [1])


def test_pending_counts_turn_unknown_with_a_fail_safe_hole():
    assert PendingSequence(T, T, 3).merge(T + S, UNKNOWN) == PendingSequence(T, T + S, UNKNOWN)


def test_context_silence_is_two_rows_of_one_run_far_enough_apart():
    """covers: spec 0009 AC-7."""
    from trading_core.marketdata.gaps import SilenceDetector, SilenceState

    s = 1_000_000_000
    detector = SilenceDetector("X", "mark_price_update", SilenceState(), silence_ns=60 * s)

    gaps = detector.scan("a", [0, 59 * s, 119 * s])  # 59 s, then exactly 60 s
    gaps += detector.scan("b", [500 * s])  # a new run: the book's recorder_down covers it

    assert gaps == [Gap(59 * s, "X", "silence", 119 * s, stream="mark_price_update")]
    assert detector.state == SilenceState(500 * s, "b")


def test_context_silence_resumes_from_its_stored_state():
    """covers: spec 0009 AC-7 (detector state kept in done.json)."""
    from trading_core.marketdata.gaps import SilenceDetector, SilenceState

    s = 1_000_000_000
    state = SilenceState.from_dict(SilenceState(10 * s, "a").to_dict())
    detector = SilenceDetector("X", "index_price_update", state, silence_ns=60 * s)

    assert detector.scan("a", [80 * s]) == [Gap(10 * s, "X", "silence", 80 * s, stream="index_price_update")]


def test_a_tail_silence_runs_from_the_last_row_to_the_run_end():
    """covers: spec 0009 AC-7 (amended: tail silence)."""
    from trading_core.marketdata.gaps import tail_silence

    s = 1_000_000_000
    oi = "custom_open_interest_update"

    assert tail_silence("X", oi, 0, 600 * s, silence_ns=60 * s) == Gap(0, "X", "silence", 600 * s, stream=oi)
    assert tail_silence("X", oi, 0, 60 * s, silence_ns=60 * s) == Gap(0, "X", "silence", 60 * s, stream=oi)
    assert tail_silence("X", oi, 0, 59 * s, silence_ns=60 * s) is None  # under the minimum
    assert tail_silence("X", oi, 600 * s, 600 * s, silence_ns=60 * s) is None  # last row at the end
    assert tail_silence("X", oi, 700 * s, 600 * s, silence_ns=60 * s) is None


def test_a_tail_silence_ends_at_the_tail_reconnect_when_that_comes_first():
    """covers: spec 0009 AC-7 (amended: the silence stops where the tail reconnect starts)."""
    from trading_core.marketdata.gaps import tail_silence

    s = 1_000_000_000
    oi = "custom_open_interest_update"

    assert tail_silence("X", oi, 0, 600 * s, silence_ns=60 * s, stop_ns=300 * s) == Gap(
        0, "X", "silence", 300 * s, stream=oi
    )
    assert tail_silence("X", oi, 0, 600 * s, silence_ns=60 * s, stop_ns=30 * s) is None
    assert tail_silence("X", oi, 0, 600 * s, silence_ns=60 * s, stop_ns=900 * s) == Gap(
        0, "X", "silence", 600 * s, stream=oi
    )


def test_a_new_run_reports_the_run_it_ended_and_its_end():
    """covers: spec 0009 AC-7 (amended: run_ends saved with the recorder_down)."""
    detector = GapDetector("X", GapState(), silence_ns=10 * S)

    first = detector.scan_book("a", [T, T + S], [True, False])
    second = detector.scan_book("b", [T + 100 * S], [True])

    assert first.ended is None
    assert second.ended == ("a", T + S)
    assert second.gaps == [Gap(T + S, "X", "recorder_down", T + 100 * S)]


def test_a_stronger_overlapping_gap_of_the_same_stream_wins():
    """covers: spec 0009 AC-7 (recorder_down over reconnect over silence, within one stream)."""
    from trading_core.marketdata.gaps import resolve_overlaps

    oi = "custom_open_interest_update"
    silence = Gap(10, "X", "silence", 100, stream=oi)
    reconnect = Gap(20, "X", "reconnect", 90, stream=oi)
    down = Gap(80, "X", "recorder_down", 200, stream=oi)
    other_stream = Gap(10, "X", "silence", 100, stream="mark_price_update")
    touching = Gap(200, "X", "silence", 260, stream=oi)

    assert resolve_overlaps([silence, reconnect, down, other_stream, touching]) == sorted(
        [down, other_stream, touching]
    )
    assert resolve_overlaps([silence, reconnect]) == [reconnect]

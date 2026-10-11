"""covers: spec 0007 AC-15 (the sequence tracker), AC-16 (the holes file, the ready gate, the release limit)."""

import json

from trading_core.marketdata.hours import HOUR_NS, SECOND_NS
from trading_core.marketdata.sequence import (
    NEVER_NS,
    UNKNOWN,
    RunHoles,
    SequenceHole,
    SequenceTracker,
    holes_ready,
    parse_holes,
    release_limit,
)

T = 1_791_439_200 * SECOND_NS
S = SECOND_NS
CONFIRM = 600 * S


def _tracker():
    return SequenceTracker(confirm_ns=CONFIRM)


def _ts(n):
    """The ts_init of delta number n: one per millisecond."""
    return T + n * 1_000_000


def _feed(tracker, numbers, now):
    return [tracker.observe(n, _ts(n), now) for n in numbers]


def test_a_hole_is_confirmed_only_after_the_confirm_time():
    tracker = _tracker()
    _feed(tracker, [n for n in range(1, 1001) if not 400 <= n <= 410], T)
    assert tracker.tick(T + CONFIRM - 1).holes == []
    holes = tracker.tick(T + CONFIRM).holes
    assert holes == [SequenceHole(400, 410, 11, _ts(399), _ts(411))]
    assert tracker.tick(T + 2 * CONFIRM).holes == []


def test_numbers_that_arrive_before_the_confirm_time_were_only_overtaken():
    tracker = _tracker()
    _feed(tracker, [n for n in range(1, 1001) if not 400 <= n <= 410], T)
    assert _feed(tracker, range(400, 411), T + 500 * S) == [False] * 11
    assert tracker.tick(T + 2 * CONFIRM).holes == []


def test_one_missing_number_is_a_hole():
    tracker = _tracker()
    _feed(tracker, [1, 2, 4, 5], T)
    assert tracker.tick(T + CONFIRM).holes == [SequenceHole(3, 3, 1, _ts(2), _ts(4))]


def test_numbers_shared_by_two_instruments_give_no_hole_when_none_is_missing():
    tracker = _tracker()
    # The counter is one per run: BTC and ETH rows interleave on it.
    btc = list(range(1, 2001, 2))
    eth = list(range(2, 2001, 2))
    _feed(tracker, [n for pair in zip(btc, eth, strict=True) for n in pair], T)
    assert tracker.tick(T + CONFIRM).holes == []


def test_a_hole_of_189_000_keeps_the_state_small():
    tracker = _tracker()
    _feed(tracker, range(1, 101), T)
    _feed(tracker, range(189_101, 200_001), T)
    assert len(tracker._lo) == 1  # one run of seen numbers, not one entry per number
    holes = tracker.tick(T + CONFIRM).holes
    assert holes == [SequenceHole(101, 189_100, 189_000, _ts(100), _ts(189_101))]


def test_the_tracker_starts_at_the_first_number_it_sees():
    tracker = _tracker()
    _feed(tracker, range(5_000, 5_100), T)
    assert _feed(tracker, [12], T) == [False]  # below the start: ignored
    assert tracker.tick(T + CONFIRM).holes == []


def test_a_filled_middle_splits_the_hole_and_both_parts_keep_its_start():
    tracker = _tracker()
    _feed(tracker, [1, 2, 10, 11], T)
    _feed(tracker, [5], T + 100 * S)
    assert tracker.tick(T + CONFIRM).holes == [
        SequenceHole(3, 4, 2, _ts(2), _ts(5)),
        SequenceHole(6, 9, 4, _ts(5), _ts(10)),
    ]


def test_filling_a_hole_from_both_ends_closes_it():
    tracker = _tracker()
    _feed(tracker, [1, 2, 6, 7, 20], T)
    _feed(tracker, [3, 5, 4], T + S)
    assert tracker.tick(T + CONFIRM).holes == [SequenceHole(8, 19, 12, _ts(7), _ts(20))]


def test_a_number_after_its_hole_was_confirmed_is_late():
    tracker = _tracker()
    _feed(tracker, [1, 2, 4], T)
    tracker.tick(T + CONFIRM)
    assert _feed(tracker, [3], T + CONFIRM + S) == [True]
    assert _feed(tracker, [2], T + CONFIRM + S) == [False]  # a repeat, not late


def test_checked_to_never_passes_the_oldest_open_suspicion():
    tracker = _tracker()
    _feed(tracker, [1, 2], T)
    assert tracker.tick(T + CONFIRM + 5 * S).checked_to_ns == T + 5 * S
    _feed(tracker, [4], T + CONFIRM + 5 * S)
    # The hole sits after number 2, so nothing past its ts_init is known to be complete yet.
    assert tracker.tick(T + 2 * CONFIRM).checked_to_ns == _ts(2)
    assert tracker.tick(T + 2 * CONFIRM + 5 * S).checked_to_ns == T + CONFIRM + 5 * S


def test_close_turns_every_open_suspicion_into_a_hole():
    tracker = _tracker()
    _feed(tracker, [1, 3, 6], T)
    assert tracker.close() == [
        SequenceHole(2, 2, 1, _ts(1), _ts(3)),
        SequenceHole(4, 5, 2, _ts(3), _ts(6)),
    ]
    assert tracker.close() == []


def _lines(*items):
    return [json.dumps(item) for item in items]


def test_parse_holes_reads_ticks_holes_and_the_close():
    hole = SequenceHole(3, 4, 2, T, T + S)
    later = SequenceHole(9, 9, 1, T + 9 * S, T + 10 * S)
    parsed = parse_holes(
        _lines({"started_ns": T}, later.to_dict(), {"checked_to_ns": T + 5 * S}, hole.to_dict(), {"closed": True})
    )
    assert parsed.holes == (hole, later)  # sorted by before_ns
    assert parsed.checked_to_ns == T + 5 * S
    assert parsed.closed
    assert parsed.complete_lines == 5
    assert parsed.bad_lines == 0


def test_parse_holes_counts_bad_lines():
    parsed = parse_holes(["{", "[]", json.dumps({"first_missing": 1}), json.dumps({"checked_to_ns": T})])
    assert parsed.bad_lines == 3
    assert parsed.checked_to_ns == T
    # Bad lines before the first tick: a fail safe hole from the start of the run to that tick.
    assert parsed.holes == (SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, 0, T),)


def test_a_garbled_line_becomes_a_hole_between_the_ticks_around_it():
    real = SequenceHole(3, 4, 2, T + 61 * S, T + 62 * S)
    lines = _lines({"started_ns": T}, {"checked_to_ns": T + 60 * S}, real.to_dict(), {"checked_to_ns": T + 120 * S})
    lines[2] = lines[2][:10] + "#" + lines[2][11:]  # the real hole is lost
    parsed = parse_holes(lines)
    assert parsed.bad_lines == 1
    assert parsed.holes == (SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, T + 60 * S, T + 120 * S),)
    assert parsed.checked_to_ns == T + 120 * S


def test_a_garbled_last_tick_waits_unless_the_run_is_closed():
    tick, close = _lines({"checked_to_ns": T + 60 * S}, {"closed": True})
    open_run = parse_holes([tick, '{"checked_to'])
    assert open_run.holes == ()
    assert open_run.checked_to_ns == T + 60 * S  # rows above it stay held back
    closed = parse_holes([tick, "garbage", close])
    assert closed.holes == (SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, T + 60 * S, NEVER_NS),)


def test_the_fail_safe_hole_covers_the_unchecked_tail():
    parsed = parse_holes(_lines({"started_ns": T}, {"checked_to_ns": T + 60 * S}))
    tail = parsed.with_fail_safe().holes[-1]
    assert tail == SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, T + 60 * S, NEVER_NS)
    # No tick written: the whole run.
    assert parse_holes([]).with_fail_safe().holes == (SequenceHole(UNKNOWN, UNKNOWN, UNKNOWN, 0, NEVER_NS),)
    # A closed run gets nothing more.
    closed = parse_holes(_lines({"closed": True}))
    assert closed.with_fail_safe() is closed


def test_an_hour_waits_for_the_holes_file_to_cover_it():
    hour = T - T % HOUR_NS
    behind = RunHoles(checked_to_ns=hour + HOUR_NS - 1)
    assert not holes_ready(hour, behind, run_ended=False)
    assert holes_ready(hour, RunHoles(checked_to_ns=hour + HOUR_NS), run_ended=False)
    assert holes_ready(hour, behind, run_ended=True)
    assert holes_ready(hour, RunHoles(closed=True), run_ended=False)
    assert not holes_ready(hour, RunHoles(), run_ended=False)  # no tick yet
    assert holes_ready(hour, None, run_ended=False)  # recorded before the sequence watch


def test_rows_past_the_checked_time_are_held_back():
    assert release_limit(RunHoles(checked_to_ns=T), run_ended=False) == T
    assert release_limit(RunHoles(), run_ended=False) == -1
    assert release_limit(RunHoles(checked_to_ns=T), run_ended=True) is None
    assert release_limit(RunHoles(closed=True), run_ended=False) is None
    assert release_limit(None, run_ended=False) is None

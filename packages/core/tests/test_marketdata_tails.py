"""covers: spec 0009 AC-6, AC-7 (amended 2026-10-16: tail silence, tail reconnect, and the done.json
maps they wait on)."""

from trading_core.marketdata.context_feed import Outage
from trading_core.marketdata.gaps import Gap
from trading_core.marketdata.tails import StreamMark, TailState, resolve_tails

S = 1_000_000_000
X = "PF_XBTUSD.KRAKEN"
OI = "custom_open_interest_update"
LIQ = "custom_liquidation_tick"
MARK = "mark_price_update"
STEADY = (MARK, OI)


def key(table):
    return f"{table}/{X}"


CURRENT_BOOK = StreamMark("b", 2_000 * S)


def resolve(state, *, book=CURRENT_BOOK, silences=None):
    return resolve_tails(
        state,
        books={X: book},
        silences=silences if silences is not None else {key(t): StreamMark("b", 1_900 * S) for t in STEADY},
        silence_tables=STEADY,
        raw_tables=(OI, LIQ),
        stop_table=OI,
        silence_ns=60 * S,
    )


def test_a_tail_silence_ends_at_the_run_end_and_everything_is_pruned():
    state = TailState(run_ends={X: {"a": 1_000 * S}}, pending_tails={key(OI): {"a": 400 * S}})

    gaps = resolve(state)

    assert gaps == [Gap(400 * S, X, "silence", 1_000 * S, stream=OI)]
    assert state == TailState()


def test_an_open_line_with_no_partner_is_a_reconnect_of_both_raw_streams():
    state = TailState(run_ends={X: {"a": 1_000 * S}}, open_outages={"a": [Outage(700 * S, None, "idle")]})

    assert resolve(state) == sorted(Gap(700 * S, X, "reconnect", 1_000 * S, stream=t) for t in (OI, LIQ))
    assert state == TailState()


def test_the_open_interest_tail_silence_stops_at_the_tail_reconnect_and_mark_does_not():
    state = TailState(
        run_ends={X: {"a": 1_000 * S}},
        open_outages={"a": [Outage(700 * S, None, "idle")]},
        pending_tails={key(OI): {"a": 400 * S}, key(MARK): {"a": 400 * S}},
    )

    gaps = resolve(state)

    assert Gap(400 * S, X, "silence", 700 * S, stream=OI) in gaps
    assert Gap(400 * S, X, "silence", 1_000 * S, stream=MARK) in gaps
    assert len(gaps) == 4


def test_a_tail_under_the_minimum_gives_nothing_but_still_resolves():
    state = TailState(run_ends={X: {"a": 1_000 * S}}, pending_tails={key(OI): {"a": 950 * S}})

    assert resolve(state) == []
    assert state == TailState()


def test_an_open_line_at_or_after_the_run_end_gives_nothing():
    state = TailState(run_ends={X: {"a": 1_000 * S}}, open_outages={"a": [Outage(1_000 * S, None, "idle")]})

    assert resolve(state) == []
    assert state.open_outages == {}


def test_a_current_run_waits():
    state = TailState(open_outages={"b": [Outage(1_500 * S, None, "idle")]}, pending_tails={key(OI): {"b": 1_400 * S}})
    before = TailState(
        dict(state.run_ends), dict(state.open_outages), {k: dict(v) for k, v in state.pending_tails.items()}
    )

    assert resolve(state, book=StreamMark("b", 2_000 * S)) == []
    assert state == before


def test_a_run_whose_steady_stream_has_not_ended_it_waits():
    """The book ended run a, but open interest is still on it: its tail may not be stored yet."""
    state = TailState(run_ends={X: {"a": 1_000 * S}}, open_outages={"a": [Outage(700 * S, None, "idle")]})
    silences = {key(OI): StreamMark("a", 400 * S), key(MARK): StreamMark("b", 1_900 * S)}

    assert resolve(state, silences=silences) == []
    assert state.run_ends == {X: {"a": 1_000 * S}}

    state.pending_tails[key(OI)] = {"a": 400 * S}  # open interest ended run a
    gaps = resolve(state, silences=silences)

    assert Gap(400 * S, X, "silence", 700 * S, stream=OI) in gaps
    assert state.open_outages == {}
    assert state.pending_tails == {}


def test_the_same_gaps_come_out_whichever_entry_lands_first():
    """Book first, then the silence stream, or the other way round, over two passes."""
    tails = {key(OI): {"a": 400 * S}}
    one = TailState(run_ends={X: {"a": 1_000 * S}})
    first = resolve(one, silences={key(OI): StreamMark("a", 400 * S)})
    one.pending_tails = {k: dict(v) for k, v in tails.items()}
    first += resolve(one)

    two = TailState(pending_tails={k: dict(v) for k, v in tails.items()})
    second = resolve(two, book=StreamMark("a", 1_000 * S))
    two.run_ends = {X: {"a": 1_000 * S}}
    second += resolve(two)

    assert first == second == [Gap(400 * S, X, "silence", 1_000 * S, stream=OI)]


def test_a_run_with_no_book_rows_is_dropped_once_the_book_moved_past_it():
    """Run m lies inside the recorder_down from a's end to b's first row, which covers it already."""
    state = TailState(
        run_ends={X: {"a": 1_000 * S}},
        open_outages={"m": [Outage(1_200 * S, None, "idle")]},
        pending_tails={key(OI): {"a": 980 * S, "m": 1_100 * S}},
    )

    assert resolve(state) == []  # a's tail is 20 s, under the minimum; m's entries are covered
    assert state == TailState()


def test_a_closed_line_already_paired_leaves_only_the_tail_silence():
    state = TailState(run_ends={X: {"a": 1_000 * S}}, pending_tails={key(OI): {"a": 300 * S}})

    assert resolve(state) == [Gap(300 * S, X, "silence", 1_000 * S, stream=OI)]


def test_no_book_yet_resolves_nothing():
    state = TailState(open_outages={"a": [Outage(10, None, "idle")]})

    assert (
        resolve_tails(
            state, books={}, silences={}, silence_tables=STEADY, raw_tables=(OI, LIQ), stop_table=OI, silence_ns=60 * S
        )
        == []
    )
    assert state.open_outages == {"a": [Outage(10, None, "idle")]}

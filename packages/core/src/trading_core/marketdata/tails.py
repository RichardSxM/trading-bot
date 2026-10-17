"""Context gaps at a run's tail (spec 0009 AC-7, amended 2026-10-16).

A hole that is still open when a run ends has no later row or closed outage line to end it, so it
ends at the run's end: the ``ts_init`` of the run's last book row, where the book's
``recorder_down`` starts. Two kinds wait on that end:

- a tail ``reconnect`` of the raw feed's streams, from an open outage line that never got its
  closed line;
- a tail ``silence`` of a steady context stream, from its last row of the run, ending at the run's
  tail ``reconnect`` instead when that starts first (open interest).

``TailState`` is what ``done.json`` keeps of them, so the result never depends on which stream a
pass converted first, on the run folder still existing, or on a pass being killed part way.
``resolve_tails`` runs at the end of every pass.

A run is resolved, per instrument, once its end is known and every steady stream of the instrument
has passed it. A run with no book rows never gets an end: it lies inside the ``recorder_down``
around it, so its entries are dropped once the book has moved on past them. An instrument whose
book never reached a run (no longer recorded) takes no part in that run's open lines.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from trading_core.marketdata.context_feed import Outage, close_at_run_end
from trading_core.marketdata.gaps import Gap, tail_silence


@dataclass(frozen=True)
class StreamMark:
    """Where a stream's detector stands: its last row and that row's run (None when it has none)."""

    run: str | None = None
    last_ts: int | None = None


@dataclass
class TailState:
    """What the tails wait on, as ``done.json`` keeps it.

    - ``run_ends``: per instrument, per run, the run's end (ns).
    - ``open_outages``: per run, its open outage lines with no closed line yet.
    - ``pending_tails``: per stream key (``<table>/<instrument>``), per run, the stream's last row
      ``ts_init`` in that run.
    """

    run_ends: dict[str, dict[str, int]] = field(default_factory=dict)
    open_outages: dict[str, list[Outage]] = field(default_factory=dict)
    pending_tails: dict[str, dict[str, int]] = field(default_factory=dict)


def _key(table: str, instrument_id: str) -> str:
    return f"{table}/{instrument_id}"


def resolve_tails(
    state: TailState,
    *,
    books: Mapping[str, StreamMark],
    silences: Mapping[str, StreamMark],
    silence_tables: Iterable[str],
    raw_tables: Iterable[str],
    stop_table: str,
    silence_ns: int,
) -> list[Gap]:
    """The tail gaps every run now resolvable gives; resolved and dropped entries leave ``state``.

    ``books`` is the book detector's mark per instrument (the instruments the tails cover), and
    ``silences`` the steady streams' marks by stream key. ``stop_table``'s tail silence ends at the
    run's tail ``reconnect`` when that starts first. Returns the gaps sorted.
    """
    steady = tuple(silence_tables)
    raw = tuple(raw_tables)
    instruments = sorted(books)
    gaps: list[Gap] = []
    if not instruments:
        return gaps  # no book converted yet: nothing can be placed

    def bookless(instrument: str, run: str, ts: int) -> bool:
        # The book moved on past this run's entries without ever ending the run: it had no book rows.
        book = books[instrument]
        return (
            run not in state.run_ends.get(instrument, {})
            and book.run is not None
            and book.run != run
            and book.last_ts is not None
            and ts < book.last_ts
        )

    def covers(instrument: str, run: str, ts: int) -> bool:
        # The book has reached this run: it ended it, is in it, or is past ts. An instrument no
        # longer recorded (its book stopped before the run) has no say in the run's open lines.
        book = books[instrument]
        return (
            run in state.run_ends.get(instrument, {})
            or book.run == run
            or (book.last_ts is not None and ts < book.last_ts)
        )

    def passed(instrument: str, run: str) -> bool:
        # Every steady stream has ended the run (its last row is stored) or is past it.
        for table in steady:
            key = _key(table, instrument)
            if run in state.pending_tails.get(key, {}):
                continue
            mark = silences.get(key)
            if mark is not None and mark.run == run:
                return False
        return True

    def ready(instrument: str, run: str) -> int | None:
        end = state.run_ends.get(instrument, {}).get(run)
        return end if end is not None and passed(instrument, run) else None

    runs = set(state.open_outages) | {run for tails in state.pending_tails.values() for run in tails}
    for run in sorted(runs):
        open_lines = state.open_outages.get(run, [])
        ends = {instrument: ready(instrument, run) for instrument in instruments}
        # Open lines are shared by every instrument the run covers: they go once each of those is
        # resolved or bookless, and wait while no book has reached the run yet.
        first = min((o.start_ns for o in open_lines), default=0)
        covering = {i: end for i, end in ends.items() if covers(i, run, first)}
        if (
            open_lines
            and covering
            and all(
                end is not None or all(bookless(instrument, run, o.start_ns) for o in open_lines)
                for instrument, end in covering.items()
            )
        ):
            for instrument, end in covering.items():
                if end is not None:
                    gaps.extend(
                        Gap(o.start_ns, instrument, "reconnect", o.end_ns, stream=table)
                        for o in close_at_run_end(open_lines, end)
                        if o.end_ns is not None
                        for table in raw
                    )
            open_done = True
        else:
            open_done = not open_lines
        for instrument in instruments:
            end = ends[instrument]
            for table in steady:
                tails = state.pending_tails.get(_key(table, instrument), {})
                last = tails.get(run)
                if last is None:
                    continue
                if end is not None:
                    stop = None
                    if table == stop_table:
                        stop = min((o.start_ns for o in open_lines if o.start_ns < end), default=None)
                    gap = tail_silence(instrument, table, last, end, silence_ns=silence_ns, stop_ns=stop)
                    if gap is not None:
                        gaps.append(gap)
                    del tails[run]
                elif bookless(instrument, run, last):
                    del tails[run]
        if open_done:
            state.open_outages.pop(run, None)
    state.pending_tails = {key: tails for key, tails in state.pending_tails.items() if tails}

    # A run's end is kept while anything of the run may still need it.
    for instrument in instruments:
        ended = state.run_ends.get(instrument, {})
        for run in [r for r in ended if ready(instrument, r) is not None and r not in state.open_outages]:
            if not any(run in state.pending_tails.get(_key(t, instrument), {}) for t in steady):
                del ended[run]
    state.run_ends = {instrument: ended for instrument, ended in state.run_ends.items() if ended}
    return sorted(gaps)

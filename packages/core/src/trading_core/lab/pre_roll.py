"""The trend kind's warmup pre roll (spec 0027.1 AC-10a), pure: where a segment's candles start being read.

A trend run reads ``warmup_bars`` bars of the same candles before each segment start, so a daily rule can
decide on the segment's first close. Those bars feed the strategy's bar aggregation and indicators only;
the lab sends no order and records no fill, funding charge, position, or measure before the segment start.
The pre roll never reaches before the coin's first stored month.
"""

from collections.abc import Callable

from trading_core.lab.candles import Month


def pre_roll_start(start_ns: int, warmup_bars: int, bar_interval_ns: int, stored: Callable[[Month], bool]) -> int:
    """``max(start - warmup_bars bars, the first stored month's start)``; ``start_ns`` for no warmup.

    ``stored`` says whether a month holds candles. Months from the pre roll's month up to the start's are
    searched in order, so a coin listed inside the pre roll starts at its first stored month (the stream
    then skips the quiet minutes before its first trade, as at any start).
    """
    if warmup_bars < 0:
        raise ValueError(f"warmup_bars must be at least 0, got {warmup_bars}")
    want = start_ns - warmup_bars * bar_interval_ns
    if want >= start_ns:
        return start_ns
    month, last = Month.of_ns(want), Month.of_ns(start_ns)
    while month < last and not stored(month):
        month = month.next()
    return max(want, month.start_ns)

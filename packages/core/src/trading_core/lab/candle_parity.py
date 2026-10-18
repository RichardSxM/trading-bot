"""The candle parity check's pure matching (spec 0014 AC-21).

``lab candle-parity`` runs one strategy over the same own order book segments twice, through the
order book unit and through the candle unit, and matches their ``enter`` decisions, approved or
refused: same side, at most ``MATCH_NS`` apart, each decision matched once, earliest first. Unlike
the paper parity of spec 0008, no decision is left out: both sides run in the lab with the same
inputs, so a refusal on one side only is a real difference.

Times are Nautilus nanosecond ints (UTC).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from trading_core.lab.parity import THRESHOLD, Decided
from trading_core.recording.enums import Intent

# Two decisions of one bar close are at most this far apart (AC-21).
MATCH_NS: Final = 60 * 1_000_000_000


@dataclass(frozen=True)
class CandleParityResult:
    """The matched pairs, how many decisions each side had, and every unmatched one."""

    matches: int
    book: int
    candles: int
    unmatched_book: tuple[Decided, ...]
    unmatched_candles: tuple[Decided, ...]

    @property
    def ratio(self) -> Decimal | None:
        """Matches ÷ max(book, candles); None when there is no decision to compare."""
        most = max(self.book, self.candles)
        return None if most == 0 else Decimal(self.matches) / Decimal(most)

    @property
    def passed(self) -> bool:
        """True when the ratio is at least 0.95."""
        ratio = self.ratio
        return ratio is not None and ratio >= THRESHOLD


def match_enters(book: Iterable[Decided], candles: Iterable[Decided], match_ns: int = MATCH_NS) -> CandleParityResult:
    """Match the ``enter`` decisions of the order book run with those of the candle run."""
    book_in = sorted((d for d in book if d.intent == Intent.ENTER), key=lambda d: d.decided_ns)
    candles_in = sorted((d for d in candles if d.intent == Intent.ENTER), key=lambda d: d.decided_ns)
    used: set[int] = set()
    unmatched_book: list[Decided] = []
    for b in book_in:
        found = next(
            (
                i
                for i, c in enumerate(candles_in)
                if i not in used and c.side == b.side and abs(c.decided_ns - b.decided_ns) <= match_ns
            ),
            None,
        )
        if found is None:
            unmatched_book.append(b)
        else:
            used.add(found)
    return CandleParityResult(
        matches=len(used),
        book=len(book_in),
        candles=len(candles_in),
        unmatched_book=tuple(unmatched_book),
        unmatched_candles=tuple(c for i, c in enumerate(candles_in) if i not in used),
    )

"""The loser check (spec 0008 AC-14): a random taker must lose what fees and spread say it should.

For each trade the expected net P&L is ``-(taker_fee * (entry notional + exit notional) +
qty * (entry spread + exit spread) / 2)`` in USD, each spread taken from the book when the
order was submitted. The check passes when the run lost money in total and the mean net P&L of
the trades used lies within ``expected ± max(3 * standard error, 0.5 bps of mean notional)``.
Trades that cross a funding boundary or close at ``segment_end`` count in the total only.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

HALF_BPS: Final = Decimal("0.00005")
SE_MULTIPLE: Final = Decimal(3)


@dataclass(frozen=True)
class BandTrade:
    """One closed round trip as the band sees it (USD, quantity in BTC)."""

    qty: Decimal
    entry_notional: Decimal
    exit_notional: Decimal
    entry_spread: Decimal
    exit_spread: Decimal
    net_pnl: Decimal
    crosses_funding: bool = False
    segment_end: bool = False

    @property
    def used(self) -> bool:
        """True when the trade counts in the band (not across funding, not a ``segment_end`` close)."""
        return not (self.crosses_funding or self.segment_end)

    def expected(self, taker_fee: Decimal) -> Decimal:
        """What fees and half the spread on each side should cost this trade, as a negative P&L."""
        fees = taker_fee * (self.entry_notional + self.exit_notional)
        spread = self.qty * (self.entry_spread + self.exit_spread) / 2
        return -(fees + spread)


@dataclass(frozen=True)
class LoserBand:
    """The check's numbers, as the report prints them. ``passed`` is False when fewer than two trades are used."""

    trades_used: int
    total_net: Decimal
    expected: Decimal | None
    mean_net: Decimal | None
    std_error: Decimal | None
    half_width: Decimal | None
    passed: bool

    @property
    def low(self) -> Decimal | None:
        """The band's lower edge."""
        return None if self.expected is None or self.half_width is None else self.expected - self.half_width

    @property
    def high(self) -> Decimal | None:
        """The band's upper edge."""
        return None if self.expected is None or self.half_width is None else self.expected + self.half_width


def _mean(values: Sequence[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / len(values)


def loser_band(trades: Sequence[BandTrade], taker_fee: Decimal) -> LoserBand:
    """Run the check over a variant's closed trades.

    "Mean notional" is the mean over the trades used of the average of entry and exit
    notional. The standard error is the sample standard deviation of the per trade net P&L
    over the square root of the count. Raises ``ValueError`` for a fee outside [0, 1).
    """
    if not Decimal(0) <= taker_fee < 1:
        raise ValueError(f"taker fee must be in [0, 1), got {taker_fee}")
    total = sum((t.net_pnl for t in trades), Decimal(0))
    used = [t for t in trades if t.used]
    if len(used) < 2:
        return LoserBand(len(used), total, None, None, None, None, passed=False)
    nets = [t.net_pnl for t in used]
    mean_net = _mean(nets)
    variance = sum(((n - mean_net) ** 2 for n in nets), Decimal(0)) / (len(nets) - 1)
    std_error = (variance / len(nets)).sqrt()
    expected = _mean([t.expected(taker_fee) for t in used])
    mean_notional = _mean([(t.entry_notional + t.exit_notional) / 2 for t in used])
    half_width = max(SE_MULTIPLE * std_error, HALF_BPS * mean_notional)
    inside = expected - half_width <= mean_net <= expected + half_width
    return LoserBand(len(used), total, expected, mean_net, std_error, half_width, passed=total < 0 and inside)

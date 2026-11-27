"""Spec 0025 AC-13a and AC-25: the spread to ATR ratio at every 5 minute bar close in the recorded catalog.

The catalog holds L2 book deltas, not quotes, so the best bid and ask at a bar close t come from the
hour's file: after the last ``CLEAR`` at or before t, the last row of each price level (by ``ts_init``)
says whether the level is alive (not a ``DELETE``, size above 0); the best bid is the highest live bid
and the best ask the lowest live ask. A bar close is skipped when a book gap started after that
``CLEAR`` and before t, when there is no ``CLEAR`` yet, or when the book is crossed or one sided.

The ATR is the strategy's own: Wilder's ATR(14) over 5 minute bars built from the recorded trades,
empty intervals skipped as the live bars do. The ratio at t uses the ATR after the bar that closes at t.
Prices are 128 bit fixed point (scale 1e16); this is an analysis, so it works in float64.

Read only. Run it in the trader image on the VPS at low priority, for example:
  nice -n 19 kfs-compose run --rm --no-deps -T -v /srv/kfs/catalog:/cat:ro -v <dir>:/spike:ro \\
    --entrypoint python trader -I /spike/spread_atr.py PF_XBTUSD.KRAKEN [hours]
"""

import sys
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

CATALOG = Path("/cat")
SCALE = 1e16
BAR_NS = 300 * 1_000_000_000
HOUR_NS = 3600 * 1_000_000_000
ATR_N = 14
LIMIT = 0.1
DELETE, CLEAR = 3, 4  # BookAction: 1 ADD, 2 UPDATE, 3 DELETE, 4 CLEAR
BID, ASK = 1, 2


def fixed(col: object) -> np.ndarray:
    """128 bit little endian fixed point values as float64 prices."""
    raw = np.frombuffer(col.combine_chunks().buffers()[1], dtype="<u8").reshape(-1, 2)  # type: ignore[attr-defined]
    hi = raw[:, 1].view("<i8").astype(np.float64)
    return (hi * 2.0**64 + raw[:, 0].astype(np.float64)) / SCALE


def hour_start(name: str) -> int:
    """The hour's start in ns from a ``2026-10-08T06-00-00-000000000Z_...`` file name."""
    day, clock = name.split("_")[0].split("T")
    h = int(clock[:2])
    return int(np.datetime64(f"{day}T{h:02d}:00:00", "ns").astype(np.int64))


def book_gaps(instrument: str) -> list[tuple[int, int]]:
    """Every book gap of the instrument as ``(start_ns, end_ns)``."""
    out: list[tuple[int, int]] = []
    for path in sorted((CATALOG / "gaps").glob("*.parquet")):
        t = pq.read_table(path).to_pylist()
        for row in t:
            if row["instrument_id"] == instrument and row.get("stream", "order_book_deltas") == "order_book_deltas":
                out.append((int(row["start_ns"]), int(row["end_ns"])))
    return out


def best(prices: np.ndarray, alive: np.ndarray, highest: bool) -> float | None:
    """The best live price of one side: the last row per level decides whether it is alive."""
    if prices.size == 0:
        return None
    rev = prices[::-1]
    _, first = np.unique(rev, return_index=True)
    live = rev[first][alive[::-1][first]]
    if live.size == 0:
        return None
    return float(live.max() if highest else live.min())


def quotes(instrument: str, gaps: list[tuple[int, int]], hours: int) -> Iterator[tuple[int, float, float]]:
    """``(t, bid, ask)`` at every 5 minute bar close the stored book can answer (the last ``hours`` files, 0 all)."""
    files = sorted((CATALOG / "data" / "order_book_deltas" / instrument).glob("*.parquet"))[-hours:]
    for path in files:
        start = hour_start(path.name)
        t = pq.read_table(path, columns=["action", "side", "price", "size", "ts_init"])
        action = t.column("action").to_numpy()
        side = t.column("side").to_numpy()
        price = fixed(t.column("price"))
        size = fixed(t.column("size"))
        ts = t.column("ts_init").to_numpy().astype(np.int64)
        clears = np.flatnonzero(action == CLEAR)
        for k in range(1, 13):
            close = start + k * BAR_NS
            upto = np.flatnonzero(ts <= close)
            if upto.size == 0:
                continue
            last = int(upto[-1])
            before = clears[clears <= last]
            if before.size == 0:
                continue
            c = int(before[-1])
            clear_ts = int(ts[c])
            if any(g0 <= close and g1 > clear_ts for g0, g1 in gaps):
                continue
            idx = np.arange(c + 1, last + 1)
            idx = idx[ts[idx] <= close]
            alive = (action[idx] != DELETE) & (size[idx] > 0)
            b_mask, a_mask = side[idx] == BID, side[idx] == ASK
            bid = best(price[idx][b_mask], alive[b_mask], highest=True)
            ask = best(price[idx][a_mask], alive[a_mask], highest=False)
            if bid is None or ask is None or bid >= ask:
                continue
            yield close, bid, ask


def atr_by_close(instrument: str) -> dict[int, float]:
    """Wilder's ATR(14) after each 5 minute trade bar, keyed by the bar's close time."""
    stamps: list[np.ndarray] = []
    prices: list[np.ndarray] = []
    for path in sorted((CATALOG / "data" / "trade_tick" / instrument).glob("*.parquet")):
        t = pq.read_table(path, columns=["price", "ts_event"])
        stamps.append(t.column("ts_event").to_numpy().astype(np.int64))
        prices.append(fixed(t.column("price")))
    ts = np.concatenate(stamps)
    px = np.concatenate(prices)
    order = np.argsort(ts, kind="stable")
    ts, px = ts[order], px[order]
    bucket = ts // BAR_NS
    edges = np.flatnonzero(np.diff(bucket)) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [ts.size]))
    out: dict[int, float] = {}
    atr: float | None = None
    seed: list[float] = []
    prev_close: float | None = None
    for s, e in zip(starts, ends, strict=True):
        seg = px[s:e]
        high, low, close = float(seg.max()), float(seg.min()), float(seg[-1])
        if prev_close is not None:
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            if atr is None:
                seed.append(tr)
                if len(seed) == ATR_N:
                    atr = sum(seed) / ATR_N
            else:
                atr = (atr * (ATR_N - 1) + tr) / ATR_N
            if atr is not None:
                out[int(bucket[s] + 1) * BAR_NS] = atr
        prev_close = close
    return out


def main(instrument: str, hours: int) -> None:
    atrs = atr_by_close(instrument)
    gaps = book_gaps(instrument)
    rows = [(t, ask - bid, atrs[t]) for t, bid, ask in quotes(instrument, gaps, hours) if t in atrs and atrs[t] > 0]
    if not rows:
        print(f"{instrument}: no bar close with both a book and an ATR")
        return
    t = np.array([r[0] for r in rows])
    spread = np.array([r[1] for r in rows])
    ratio = spread / np.array([r[2] for r in rows])
    first, last = (str(np.datetime64(int(x), "ns"))[:16] for x in (t.min(), t.max()))
    print(f"{instrument}: {ratio.size} bar closes from {first} to {last} UTC, {len(gaps)} book gaps")
    for q in (50, 90, 99):
        print(f"  p{q} spread/ATR {np.percentile(ratio, q):.4f}  spread {np.percentile(spread, q):.10g}")
    print(f"  mean spread/ATR {ratio.mean():.4f}, max {ratio.max():.4f}")
    print(f"  share above {LIMIT} (would skip): {(ratio > LIMIT).mean():.4%} ({int((ratio > LIMIT).sum())} closes)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "PF_XBTUSD.KRAKEN", int(sys.argv[2]) if len(sys.argv) > 2 else 0)

"""Spec 0014 Milestone 1 gate spike. Throwaway: never merged as product code.

Checks what spec 0014 assumes about Kraken's public history and the NautilusTrader 1.231.0 engine:
  candles  G-1: page the charts endpoint for whole months (one per year), page size, paging field,
           earliest minute, no trade minutes, and every price and volume decimal against the pinned
           tick 1 and step 0.0001 (AC-1, AC-3, AC-4)
  funding  G-2: the historical funding endpoint and the charts `funding` analytics: how far back each
           goes, the field meaning, and which period a timestamp names, compared with a Tardis
           derivative_ticker day (AC-2, AC-15)
  engine   G-3: the AC-8 venue fed trade then quote path points (AC-8 to AC-11)

Run candles and funding with the host python (standard library only):
  python3 scripts/spikes/0014_gate.py candles 2022-04 2023-06 2024-06 2025-06 2026-09
  python3 scripts/spikes/0014_gate.py funding tardis/cryptofacilities_derivative_ticker_2026-10-01_PF_XBTUSD.csv.gz
Run engine in the trader image:
  docker run --rm --memory 3g -v "$PWD/scripts/spikes:/spike" kfs/trader:dev python /spike/0014_gate.py engine
"""

import csv
import gzip
import json
import sys
import time
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

BASE = "https://futures.kraken.com"
TICK = Decimal(1)
STEP = Decimal("0.0001")


def _get(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
        return json.loads(resp.read())


def _month_bounds(month: str) -> tuple[int, int]:
    year, mon = (int(p) for p in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp())


def _decimals(text: str) -> int:
    return -Decimal(text).as_tuple().exponent if "." in text else 0


def candles(months: list[str]) -> None:
    for month in months:
        start, end = _month_bounds(month)
        since, pages, rows, began = start, 0, {}, time.monotonic()
        dup_same = dup_diff = 0
        while since < end:
            body = _get(f"{BASE}/api/charts/v1/trade/PF_XBTUSD/1m?from={since}&to={end}")
            pages += 1
            page = body["candles"]
            for c in page:
                t = c["time"]
                if t in rows:
                    dup_same += rows[t] == c
                    dup_diff += rows[t] != c
                if start * 1000 <= t < end * 1000:
                    rows[t] = c
            if not page or not body.get("more_candles"):
                break
            since = page[-1]["time"] // 1000 + 60
            time.sleep(0.25)
        minutes = sorted(rows)
        off_tick = [(t, f) for t in minutes for f in ("open", "high", "low", "close") if Decimal(rows[t][f]) % TICK]
        off_step = [t for t in minutes if Decimal(rows[t]["volume"]) % STEP]
        price_dec = Counter(_decimals(rows[t][f]) for t in minutes for f in ("open", "high", "low", "close"))
        vol_dec = Counter(_decimals(rows[t]["volume"]) for t in minutes)
        zero_vol = sum(Decimal(rows[t]["volume"]) == 0 for t in minutes)
        flat_zero = sum(
            Decimal(rows[t]["volume"]) == 0 and len({rows[t][f] for f in ("open", "high", "low", "close")}) == 1
            for t in minutes
        )
        not_whole = sum(t % 60_000 != 0 for t in minutes)
        bad_hl = sum(
            Decimal(rows[t]["high"]) < max(Decimal(rows[t]["open"]), Decimal(rows[t]["close"]))
            or Decimal(rows[t]["low"]) > min(Decimal(rows[t]["open"]), Decimal(rows[t]["close"]))
            for t in minutes
        )
        expected = (end - start) // 60
        gaps = []
        prev = start * 1000 - 60_000
        for t in [*minutes, end * 1000]:
            missing = (t - prev) // 60_000 - 1
            if missing >= 5:
                gaps.append(missing)
            prev = t
        first = datetime.fromtimestamp(minutes[0] / 1000, UTC) if minutes else None
        print(
            json.dumps(
                {
                    "month": month,
                    "pages": pages,
                    "rows": len(minutes),
                    "expected_minutes": expected,
                    "first_minute": str(first),
                    "dup_same": dup_same,
                    "dup_diff": dup_diff,
                    "not_whole_minute": not_whole,
                    "high_low_bad": bad_hl,
                    "off_tick": len(off_tick),
                    "off_tick_sample": off_tick[:5],
                    "off_step": len(off_step),
                    "price_decimals": dict(price_dec),
                    "volume_decimals": dict(vol_dec),
                    "zero_volume": zero_vol,
                    "zero_volume_flat": flat_zero,
                    "gaps_5plus": len(gaps),
                    "gap_minutes": sum(gaps),
                    "seconds": round(time.monotonic() - began, 1),
                }
            ),
            flush=True,
        )


def funding(tardis_ticker: str | None) -> None:
    body = _get(f"{BASE}/derivatives/api/v4/historicalfundingrates?symbol=PF_XBTUSD")
    rates = body["rates"]
    stamps = [datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) for r in rates]
    steps = Counter(int((b - a).total_seconds()) for a, b in pairwise(stamps))
    print(
        json.dumps(
            {
                "historical_rows": len(rates),
                "first": str(stamps[0]),
                "last": str(stamps[-1]),
                "server_time": body.get("serverTime"),
                "step_seconds": dict(steps),
                "not_whole_hour": sum(s.minute or s.second for s in stamps),
                "rel_types": sorted({type(r["relativeFundingRate"]).__name__ for r in rates}),
            }
        )
    )
    by_hour = {s: Decimal(str(r["relativeFundingRate"])) for s, r in zip(stamps, rates, strict=True)}
    if tardis_ticker:
        # Tardis derivative_ticker: funding_timestamp names when the current funding_rate is charged.
        seen: dict[int, Decimal] = {}
        with gzip.open(tardis_ticker, "rt") as fh:
            for row in csv.DictReader(fh):
                if row.get("funding_rate") and row.get("funding_timestamp"):
                    seen[int(row["funding_timestamp"])] = Decimal(row["funding_rate"])
        for fts, rate in sorted(seen.items())[:6]:
            at = datetime.fromtimestamp(fts / 1_000_000, UTC)
            prev = by_hour.get(at.replace(hour=(at.hour - 1) % 24)) if at.hour else None
            print(
                json.dumps(
                    {
                        "tardis_funding_ts": str(at),
                        "tardis_rate": str(rate),
                        "kraken_stamp_same": str(by_hour.get(at)),
                        "kraken_stamp_hour_before": str(prev),
                    }
                )
            )


def main() -> None:
    check, args = sys.argv[1], sys.argv[2:]
    if check == "candles":
        candles(args)
    elif check == "funding":
        funding(args[0] if args else None)
    elif check == "engine":
        from engine_gate import engine

        engine()
    else:
        raise SystemExit(f"unknown check {check}")


if __name__ == "__main__":
    main()

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
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise

BASE = "https://futures.kraken.com"
TICK = Decimal(1)
STEP = Decimal("0.0001")
NOISE = Decimal("1e-8")


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
        # G-1b: the AC-1 rounding rule (within 1e-8 of a step) and the quiet rule (volume 0 after
        # rounding and flat at the previous close).
        rounded, too_far, quiet, quiet_not_prev, zero_not_flat = 0, [], 0, [], 0
        prev_close: str | None = None
        for t in minutes:
            c = rows[t]
            vol = Decimal(c["volume"])
            snapped = vol.quantize(STEP, rounding=ROUND_HALF_EVEN)
            if snapped != vol:
                if abs(snapped - vol) <= NOISE:
                    rounded += 1
                else:
                    too_far.append((t, c["volume"]))
            flat = len({Decimal(c[f]) for f in ("open", "high", "low", "close")}) == 1
            if snapped == 0 and flat:
                quiet += 1
                if prev_close is not None and Decimal(c["close"]) != Decimal(prev_close):
                    quiet_not_prev.append((t, c["close"], prev_close))
            elif snapped == 0:
                zero_not_flat += 1
            prev_close = c["close"]
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
                    "g1b_volume_rounded": rounded,
                    "g1b_volume_too_far": len(too_far),
                    "g1b_too_far_sample": too_far[:5],
                    "g1b_quiet": quiet,
                    "g1b_quiet_not_at_prev_close": len(quiet_not_prev),
                    "g1b_quiet_not_prev_sample": quiet_not_prev[:5],
                    "g1b_zero_volume_not_flat": zero_not_flat,
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


HOURLY_FROM = datetime(2022, 9, 29, 12, tzinfo=UTC)


def _export_rows(path: str) -> dict[datetime, tuple[Decimal, Decimal]]:
    """Kraken's funding export: stamp -> (absolute_rate, relative_rate) for PF_XBTUSD."""
    import io
    import zipfile

    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            text = zf.read("exports/PF_XBTUSD.csv").decode()
    else:
        with open(path) as fh:
            text = fh.read()
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        stamp = datetime.fromisoformat(row["timestamp"]).replace(tzinfo=UTC)
        out[stamp] = (Decimal(row["absolute_rate"]), Decimal(row["relative_rate"]))
    return out


def _tardis_periods(path: str) -> tuple[list[dict], list]:
    """Per funding_timestamp: the rate Tardis shows for it, and the premium and mark over time windows."""
    ticks: list[tuple[int, int, Decimal, Decimal, Decimal]] = []
    with gzip.open(path, "rt") as fh:
        for row in csv.DictReader(fh):
            if row["funding_rate"] and row["funding_timestamp"] and row["mark_price"] and row["index_price"]:
                ticks.append(
                    (
                        int(row["timestamp"]),
                        int(row["funding_timestamp"]),
                        Decimal(row["funding_rate"]),
                        Decimal(row["mark_price"]),
                        Decimal(row["index_price"]),
                    )
                )
    by_fts: dict[int, list] = {}
    for t in ticks:
        by_fts.setdefault(t[1], []).append(t)
    return [{"fts": fts, "ticks": rows} for fts, rows in sorted(by_fts.items())], ticks


def _mean(xs: list[Decimal]) -> Decimal | None:
    return sum(xs) / len(xs) if xs else None


def _median(xs: list[Decimal]) -> Decimal | None:
    s = sorted(xs)
    return s[len(s) // 2] if s else None


def funding4h(export: str, tardis_files: list[str]) -> None:
    """G-4: is a 4 h era relative_rate charged once per 4 h period, or a per hour rate?"""
    rows = _export_rows(export)
    stamps = sorted(rows)
    four = [s for s in stamps if s < HOURLY_FROM]
    hourly = [s for s in stamps if s >= HOURLY_FROM]
    steps_4h = Counter(int((b - a).total_seconds()) for a, b in pairwise(four))
    print(
        json.dumps(
            {
                "export_rows": len(stamps),
                "four_h_rows": len(four),
                "four_h_steps": dict(steps_4h),
                "first": str(stamps[0]),
                "last_4h": str(four[-1]),
                "first_hourly": str(hourly[0]),
                "median_abs_rel_4h_era": str(_median([abs(rows[s][1]) for s in four])),
                "median_abs_rel_hourly_era": str(_median([abs(rows[s][1]) for s in hourly])),
                "median_abs_rel_hourly_2022q4": str(
                    _median([abs(rows[s][1]) for s in hourly if s < datetime(2023, 1, 1, tzinfo=UTC)])
                ),
                "median_abs_over_rel_x_price_hint": "absolute_rate / relative_rate = quote price",
            }
        ),
        flush=True,
    )
    for path in tardis_files:
        periods, ticks = _tardis_periods(path)
        out = []
        for p in periods:
            fts = p["fts"]
            at = datetime.fromtimestamp(fts / 1_000_000, UTC)
            period = timedelta(hours=4) if at <= HOURLY_FROM else timedelta(hours=1)
            stamp = at - period
            # The rate Tardis shows while counting down to fts: the most common value in the group.
            rate = Counter(t[2] for t in p["ticks"]).most_common(1)[0][0]
            p_ns = int(period.total_seconds() * 1_000_000)
            # Premium over the period before the stamp (the TWAP that sets the rate) and over the
            # period the rate applies to.
            prev_prem = _mean([(m - i) / i for ts, _, _, m, i in ticks if fts - 2 * p_ns <= ts < fts - p_ns])
            same_prem = _mean([(m - i) / i for ts, _, _, m, i in ticks if fts - p_ns <= ts < fts])
            mark = _mean([m for ts, _, _, m, _ in ticks if fts - p_ns <= ts < fts])
            exp = rows.get(stamp)
            entry = {
                "charge_at": str(at),
                "tardis_rate": str(rate),
                "export_stamp": str(stamp),
                "export_abs": None if exp is None else str(exp[0]),
                "export_rel": None if exp is None else str(exp[1]),
                "abs_over_rel": None if exp is None or not exp[1] else f"{exp[0] / exp[1]:.1f}",
                "mark": None if mark is None else f"{mark:.1f}",
                "rel_over_prev_premium": None if exp is None or not prev_prem else f"{exp[1] / prev_prem:.4f}",
                "rel_over_same_premium": None if exp is None or not same_prem else f"{exp[1] / same_prem:.4f}",
            }
            out.append(entry)
        print(json.dumps({"file": path.rsplit("/", 1)[-1], "periods": out}), flush=True)


def main() -> None:
    check, args = sys.argv[1], sys.argv[2:]
    if check == "candles":
        candles(args)
    elif check == "funding":
        funding(args[0] if args else None)
    elif check == "funding4h":
        funding4h(args[0], args[1:])
    elif check == "engine":
        from engine_gate import engine

        engine()
    elif check == "engine-b":
        from engine_gate import engine_b

        engine_b()
    else:
        raise SystemExit(f"unknown check {check}")


if __name__ == "__main__":
    main()

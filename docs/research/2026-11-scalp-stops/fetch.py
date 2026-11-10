"""Fetch Kraken Futures trade candles into npy files (research only).

``python fetch.py SYMBOL...`` stores 1 minute candles from 2025-01-01 under data/.
``python fetch.py --hourly SYMBOL...`` stores 1 hour candles from 2023-09-01 under data/h1/ (portfolio.py).
"""

import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

BASE = "https://futures.kraken.com/api/charts/v1/trade/{}/{}?from={}&to={}"


def get(url):
    last: Exception | None = None
    for attempt in range(8):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "candle-research/1.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except OSError as exc:  # retry with backoff
            time.sleep(min(60, 2**attempt))
            last = exc
    raise RuntimeError(f"gave up on {url}") from last


def fetch(symbol, start_s, end_s, resolution="1m", step_s=60):
    rows, since = [], start_s
    while since < end_s:
        body = get(BASE.format(symbol, resolution, since, end_s))
        page = body.get("candles", [])
        if not page:
            break
        for c in page:
            rows.append(
                (
                    c["time"] // 1000,
                    float(c["open"]),
                    float(c["high"]),
                    float(c["low"]),
                    float(c["close"]),
                    float(c["volume"]),
                )
            )
        last = page[-1]["time"] // 1000
        if last + step_s <= since or not body.get("more_candles", False):
            break
        since = last + step_s
        time.sleep(0.15)
    a = np.array(rows, dtype=np.float64)
    _, idx = np.unique(a[:, 0], return_index=True)
    return a[idx]


if __name__ == "__main__":
    hourly = "--hourly" in sys.argv
    start = int(
        datetime(2023, 9, 1, tzinfo=UTC).timestamp() if hourly else datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    )
    end = int(datetime(2026, 11, 10, tzinfo=UTC).timestamp())
    out = Path(__file__).parent / "data" / ("h1" if hourly else "")
    out.mkdir(parents=True, exist_ok=True)
    for sym in (arg for arg in sys.argv[1:] if arg != "--hourly"):
        t0 = time.time()
        a = fetch(sym, start, end, "1h", 3600) if hourly else fetch(sym, start, end)
        np.save(out / f"{sym}.npy", a)
        print(
            sym,
            len(a),
            datetime.fromtimestamp(a[0, 0], UTC),
            datetime.fromtimestamp(a[-1, 0], UTC),
            f"{time.time() - t0:.0f}s",
            flush=True,
        )

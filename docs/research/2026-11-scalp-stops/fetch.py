"""Fetch Kraken Futures 1m trade candles into npy files (research only)."""

import json
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

BASE = "https://futures.kraken.com/api/charts/v1/trade/{}/1m?from={}&to={}"


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


def fetch(symbol, start_s, end_s):
    rows, since = [], start_s
    while since < end_s:
        body = get(BASE.format(symbol, since, end_s))
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
        if last + 60 <= since or not body.get("more_candles", False):
            break
        since = last + 60
        time.sleep(0.15)
    a = np.array(rows, dtype=np.float64)
    _, idx = np.unique(a[:, 0], return_index=True)
    return a[idx]


if __name__ == "__main__":
    start = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 11, 10, tzinfo=UTC).timestamp())
    for sym in sys.argv[1:]:
        t0 = time.time()
        a = fetch(sym, start, end)
        out = Path(__file__).parent / "data"
        out.mkdir(exist_ok=True)
        np.save(out / f"{sym}.npy", a)
        print(
            sym,
            len(a),
            datetime.fromtimestamp(a[0, 0], UTC),
            datetime.fromtimestamp(a[-1, 0], UTC),
            f"{time.time() - t0:.0f}s",
            flush=True,
        )

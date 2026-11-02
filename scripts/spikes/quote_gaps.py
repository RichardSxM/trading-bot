"""Spec 0026 AC-19 quote gap check: which candidate coins go 10 s without a quote.

A Nautilus node with the Kraken Futures data client only (the recorder's ``build_config`` pattern: public
endpoints, no execution client, the env guard run with none) and one actor that subscribes to quote ticks
for the 10 candidates. It reads no key or secret file and touches no Redis or Postgres.

For each coin it counts what ``trading_core/node/stale.py`` checks, gaps between ``QuoteTick.ts_event``
values: from the first quote of any coin to this coin's first quote, between its quotes, and from its last
quote to the node clock at the end. A coin passes when no gap is longer than 10 s (strictly greater than,
as the stale check). A coin that never quotes fails with the whole window as its gap.

The full run is 3600 s and must start between 02:00 and 03:00 UTC (exit 64 otherwise). A shorter
``--seconds`` run is a smoke test and writes ``quote_gaps-<UTC date>-smoke.json`` instead.
Run in the trader image on the laptop:
  docker run --rm -e TRADING_ENV=demo -v "$PWD/scripts/spikes:/spike:ro" \
    -v "$PWD/lab-reports/scratch:/out" --entrypoint python kfs/trader:<tag> -I /spike/quote_gaps.py --out /out
Unit test (the gap counter):
  docker run --rm -v "$PWD/scripts/spikes:/spike:ro" -w /spike --entrypoint python kfs/trader:<tag> \
    -I -m pytest -q -p no:cacheprovider /spike/test_quote_gaps.py
"""

import argparse
import json
import os
import signal
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

NS_PER_S = 1_000_000_000
LIMIT_S = 10  # RISK_STALE_QUOTE_S, the compose default the VPS runs
FULL_RUN_S = 3600
WINDOW_HOUR_UTC = 2  # the run starts between 02:00 and 03:00 UTC
EXIT_USAGE = 64
CANDIDATES = (
    "PF_XBTUSD.KRAKEN",
    "PF_ETHUSD.KRAKEN",
    "PF_SOLUSD.KRAKEN",
    "PF_XRPUSD.KRAKEN",
    "PF_LINKUSD.KRAKEN",
    "PF_UNIUSD.KRAKEN",
    "PF_ZECUSD.KRAKEN",
    "PF_TRXUSD.KRAKEN",
    "PF_DOGEUSD.KRAKEN",
    "PF_QNTUSD.KRAKEN",
)


@dataclass(frozen=True)
class CoinGaps:
    quotes: int
    longest_gap_ns: int
    gaps_over_limit: int

    @property
    def passed(self) -> bool:
        return self.quotes > 0 and self.gaps_over_limit == 0


def count_gaps(quote_ns: Sequence[int], start_ns: int, end_ns: int, limit_ns: int) -> CoinGaps:
    """The gaps of one coin's quote times, with the start and end of the window as edges.

    No quotes at all is one gap, the whole window.
    """
    edges = [start_ns, *quote_ns, end_ns]
    gaps = [b - a for a, b in pairwise(edges)]
    return CoinGaps(
        quotes=len(quote_ns),
        longest_gap_ns=max(gaps),
        gaps_over_limit=sum(1 for gap in gaps if gap > limit_ns),
    )


def seconds(ns: int) -> str:
    return str((Decimal(ns) / NS_PER_S).quantize(Decimal("0.001")))


def utc(ns: int) -> str:
    return datetime.fromtimestamp(ns // NS_PER_S, UTC).isoformat()


def report(
    quotes: dict[str, list[int]], loaded: set[str], started_ns: int, end_ns: int, run_s: int
) -> dict[str, object]:
    """The JSON report: per coin counts and pass or fail, plus the passing set."""
    first = [ts[0] for ts in quotes.values() if ts]
    start_ns = min(first) if first else started_ns
    coins: dict[str, object] = {}
    passing: list[str] = []
    for coin in CANDIDATES:
        gaps = count_gaps(quotes.get(coin, []), start_ns, end_ns, LIMIT_S * NS_PER_S)
        coins[coin] = {
            "loaded": coin in loaded,
            "quotes": gaps.quotes,
            "longest_gap_s": seconds(gaps.longest_gap_ns),
            "gaps_over_limit": gaps.gaps_over_limit,
            "pass": gaps.passed,
        }
        if gaps.passed:
            passing.append(coin)
    return {
        "started_utc": utc(started_ns),
        "first_quote_utc": utc(start_ns) if first else None,
        "ended_utc": utc(end_ns),
        "run_s": run_s,
        "limit_s": LIMIT_S,
        "coins": coins,
        "passing": passing,
    }


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, default=FULL_RUN_S)
    parser.add_argument("--out", type=Path, default=Path("lab-reports/scratch"))
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    if args.seconds == FULL_RUN_S and now.hour != WINDOW_HOUR_UTC:
        print(f"the full run starts between 02:00 and 03:00 UTC; it is {now:%H:%M} UTC", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    suffix = "" if args.seconds == FULL_RUN_S else "-smoke"
    out = args.out / f"quote_gaps-{now:%Y-%m-%d}{suffix}.json"
    run(args.seconds, out)


def run(run_s: int, out: Path) -> None:
    # Nautilus imports stay here, so the gap counter's test needs no node.
    from nautilus_trader.adapters.kraken import KRAKEN, KrakenLiveDataClientFactory
    from nautilus_trader.common.actor import Actor
    from nautilus_trader.common.events import TimeEvent
    from nautilus_trader.config import (
        ActorConfig,
        CacheConfig,
        LiveDataEngineConfig,
        LiveExecEngineConfig,
        LoggingConfig,
        TradingNodeConfig,
    )
    from nautilus_trader.live.node import TradingNode
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.identifiers import InstrumentId, TraderId

    from trading_core import env_guard
    from trading_core.naming import node_id
    from trading_core.node.kraken import futures_data_client

    env = os.environ.get("TRADING_ENV")
    if env != "demo":
        print(f"TRADING_ENV must be demo, got {env!r}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    env_guard.check_environ("quote_gaps")

    class GapConfig(ActorConfig, frozen=True):
        run_s: int

    class GapActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
        def __init__(self, config: GapConfig, done: Callable[[dict[str, object]], None]) -> None:
            super().__init__(config)
            self._run_s = config.run_s
            self._done = done
            self._quotes: dict[str, list[int]] = {coin: [] for coin in CANDIDATES}
            self._started_ns = 0

        def on_start(self) -> None:
            self._started_ns = self.clock.timestamp_ns()
            for coin in CANDIDATES:
                self.subscribe_quote_ticks(InstrumentId.from_str(coin))
            self.clock.set_time_alert_ns("quote_gaps_end", self._started_ns + self._run_s * NS_PER_S, self._on_end)

        def on_quote_tick(self, tick: QuoteTick) -> None:
            self._quotes[str(tick.instrument_id)].append(tick.ts_event)

        def _on_end(self, event: TimeEvent) -> None:
            loaded = {coin for coin in CANDIDATES if self.cache.instrument(InstrumentId.from_str(coin)) is not None}
            self._done(report(self._quotes, loaded, self._started_ns, self.clock.timestamp_ns(), self._run_s))

    config = TradingNodeConfig(
        trader_id=TraderId(node_id("QUOTEGAPS", "demo")),
        logging=LoggingConfig(log_level="WARNING", log_colors=False),
        cache=CacheConfig(),
        data_engine=LiveDataEngineConfig(),
        exec_engine=LiveExecEngineConfig(reconciliation=False),
        data_clients={KRAKEN: futures_data_client()},
    )
    env_guard.check_exec_clients(config.exec_clients, process="recorder", env="demo")

    def done(result: dict[str, object]) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2), flush=True)
        # The result is on disk first; then stop the node as Ctrl+C would.
        os.kill(os.getpid(), signal.SIGINT)

    node = TradingNode(config=config)
    node.trader.add_actor(GapActor(GapConfig(run_s=run_s), done))
    node.add_data_client_factory(KRAKEN, KrakenLiveDataClientFactory)
    node.build()
    try:
        node.run()
    finally:
        node.dispose()
    if not out.exists():
        print("the run ended before its report was written", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

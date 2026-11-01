"""covers: spec 0027 build plan task 4 (the thin thread): a four coin ``tsmom`` fixture study through ``lab study``,
``lab holdout``, and the gate's evaluation; spec 0027.1 AC-5, AC-6, AC-8, AC-10 (``lab screen`` refuses it), and
AC-10a (no development or holdout row before its segment start); spec 0027.2 AC-13, AC-17.

Short on purpose (each coin day costs seconds): 11 development days and the holdout from 2025-10-19 to the
catalog's last whole day, with ``tsmom`` at lookback 2 and ATR 2, so each segment has a 5 day pre roll. The
walk's 10 minute hole falls in the development pre roll, so its ``stale_feed`` must not reach the run.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from lab.candle_fetch import candle_tree
from lab.screen import EXIT_USAGE as SCREEN_USAGE
from lab.screen import run_screen
from lab.study import EXIT_OK, StudyStore, load_study, run_holdout, run_study
from trading_core.gate.verdict import evaluate_candidate
from trading_core.lab.candles import FundingRow, Month, MonthManifest
from trading_core.marketdata.hours import HOUR_NS
from trading_core.recording.bundle_codec import parse_bundle
from trading_core.recording.gate_queries import study_evidence
from trading_core.recording.rows import RunBundle
from trading_core.secrets import POSTGRES_PASSWORD, read_secret
from trading_core.settings import Settings

from .candle_fixtures import walk
from .conftest import lab_settings

D = Decimal
OCT = Month(2025, 10)
DAYS = 25
TODAY = date(2026, 10, 20)
COINS = {"PF_ETHUSD.KRAKEN": 3, "PF_SOLUSD.KRAKEN": 5, "PF_XBTUSD.KRAKEN": 11, "PF_XRPUSD.KRAKEN": 13}
STUDY = """
instrument_ids = ["PF_ETHUSD.KRAKEN", "PF_SOLUSD.KRAKEN", "PF_XBTUSD.KRAKEN", "PF_XRPUSD.KRAKEN"]
from = 2025-10-08
to = 2025-10-18
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "20000"
max_leverage = "2"
max_total_exposure = "20000"

[strategy]
name = "tsmom"
[strategy.config]
bar_spec = "1-DAY-LAST"
atr_period = 2
atr_floor_mult = "1"
time_stop_bars = 30
stop_atr = "STOP"
long_only = true

[strategy.grid]
lookback_days = [2]

[study]
name = "NAME"
lab = "candle"
seed = 42
"""


def _catalog(root: Path) -> None:
    """25 walked October days per coin, each its own seed, with hourly funding at 3 bps a day."""
    for coin, seed in COINS.items():
        tree = candle_tree(root, coin)
        rows = walk(seed, DAYS * 1440, OCT.start_ns, first=3000)
        tree.write_bars(OCT, rows)
        rate = D("0.0000125")
        tree.write_funding(
            OCT, [FundingRow(OCT.start_ns + h * HOUR_NS, rate, HOUR_NS, "api") for h in range(DAYS * 24 + 1)]
        )
        tree.write_gaps(OCT, [])
        manifest = MonthManifest(
            str(OCT),
            len(rows),
            0,
            0,
            0,
            0,
            0,
            [],
            rows[0].ts_ns,
            rows[-1].ts_ns,
            0,
            0,
            0,
            False,
            "2025-11-01T00:00:00Z",
        )
        tree.write_manifest(manifest.with_funding(tree.read_funding(OCT)))


def _bundles(reports: Path) -> list[RunBundle]:
    return [parse_bundle(p.read_text()) for p in sorted(reports.rglob("*.bundle.json"))]


@pytest.mark.postgres
def test_a_four_coin_tsmom_study_runs_through_the_holdout_and_the_gate(tmp_path):
    """Runs in the lab service: ``docker compose --profile lab run --rm -T --entrypoint pytest lab -q -m postgres``."""
    catalog, reports = tmp_path / "catalog", tmp_path / "reports"
    _catalog(catalog)
    settings = lab_settings(catalog, reports).model_copy(update={"git_commit": uuid4().hex + uuid4().hex[:8]})
    name = f"tsmom-{uuid4().hex[:8]}"
    # A frozen version spends its holdout forever, and this database persists: a fresh config each run.
    stop = f"2.{uuid4().int % 1000:03d}"
    base = Settings(trading_env="demo", postgres_user="lab_app")
    store = StudyStore(create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=2))
    try:
        study = load_study_text(tmp_path, STUDY.replace("NAME", name).replace("STOP", stop))
        assert study.thresholds.version == 3
        assert run_study(settings, study, store) == EXIT_OK
        assert run_holdout(settings, name, store, today=TODAY) == EXIT_OK

        bundles = _bundles(reports)
        roles = sorted((b.study_run.role if b.study_run else "", b.strategy_name or "") for b in bundles)
        # A trend study's only benchmark is buy_and_hold (AC-8).
        assert roles == [("benchmark", "buy_and_hold"), ("development", "tsmom"), ("holdout", "tsmom")]
        for bundle in bundles:
            assert bundle.run.data_start_at is not None
            start = bundle.run.data_start_at
            assert {i.instrument_id for i in bundle.instruments} == set(COINS)  # one run, one account, four coins
            assert all(d.decided_at >= start for d in bundle.decisions)
            assert all(f.filled_at >= start for f in bundle.fills)
            assert all(r.occurred_at >= start for r in bundle.risk_events)
            assert min(d.day for d in bundle.daily_equity) >= start.date()
        holdout = next(b for b in bundles if b.study_run and b.study_run.role == "holdout")
        assert holdout.run.data_start_at is not None
        assert holdout.run.data_start_at.date().isoformat() == "2025-10-19"
        assert holdout.version is not None
        assert holdout.version.config["long_only"] is True

        with store.session() as session:
            evidence = study_evidence(session, name)
        assert (evidence.inputs.strategy_kind, evidence.inputs.long_only) == ("trend", True)
        assert evidence.inputs.prior_trials == 13
        assert evidence.inputs.trial_count >= 13 + 1
        result = evaluate_candidate(evidence.inputs)
        assert result.verdict.value in {"pass", "fail", "insufficient"}
        assert result.metrics["direction_applicable"] is False
        assert result.metrics["top_trades_applicable"] is False
        assert result.metrics["prior_trials"] == 13
        assert "beats_macd_baseline_applicable" not in result.metrics
        assert "concentration_direction" not in {r.check for r in result.reasons}
        assert "concentration_top_trades" not in {r.check for r in result.reasons}

        # lab screen takes scalping studies only (AC-10).
        assert run_screen(settings, name, store.engine) == SCREEN_USAGE
    finally:
        store.engine.dispose()


def load_study_text(tmp_path: Path, text: str):
    path = tmp_path / "study.toml"
    path.write_text(text)
    return load_study(path)

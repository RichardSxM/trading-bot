"""covers: spec 0008 AC-15 (the parity run file with [paper], the paper CSV, and `lab parity` over the fixture hours:
a CSV built from the replay's own decisions scores 1.0, a missing decision fails, a run with another config is
left out with its differing keys named).

The fixture is 17:55 to 18:05 UTC of real own recorder data, so the placeholder runs a 5 minute beat here.
"""

import io
import json
import logging
from pathlib import Path

import pytest

from lab.catalog import SourceTree
from lab.main import main
from lab.paper_csv import COLUMNS, PaperData, PaperDecision, finish_export, read_paper_csv
from lab.parity import parity, run_units
from lab.replay import plan_run, unit_plans
from lab.runfile import Overrides, RunFileError, load_run
from lab.units import run_unit
from trading_core.lab.parity import Decided, PaperRun, ParityInputError, config_sha256
from trading_core.lab.variants import VariantSpec

from .conftest import FIXTURES, H17, HOUR, lab_settings

RUNS = Path(__file__).parents[1] / "runs"
PAPER_FILE = RUNS / "placeholder_paper.toml"
# A beat the 10 minute fixture can show: boundaries every 5 minutes, 1 bar of lookback.
FAST = (
    PAPER_FILE.read_text()
    .replace("placeholder_interval_min = 15", "placeholder_interval_min = 5")
    .replace("placeholder_hold_min = 10", "placeholder_hold_min = 2")
    .replace("placeholder_lookback_min = 5", "placeholder_lookback_min = 1")
)


def _write(tmp_path: Path, text: str, name: str = "run.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _settings(tmp_path: Path):
    return lab_settings(FIXTURES, tmp_path).model_copy(update={"lab_workers": 1})


# The run file ------------------------------------------------------------------------------


def test_the_paper_run_file_gives_the_placeholder_its_settings_and_limits():
    run = load_run(PAPER_FILE, Overrides()).run

    values = run.strategy_values()
    assert (values["interval_min"], values["hold_min"], values["lookback_min"]) == (15, 10, 5)
    assert values["stop_pct"] == "0.3"
    assert values["stop_accept_timeout_s"] == "5"
    assert str(run.risk_limits().max_order_notional) == "250"
    assert run.version_config() == run.paper


def test_paper_beside_risk_or_for_another_strategy_is_refused(tmp_path):
    beside = PAPER_FILE.read_text().replace(
        "[paper]", '[risk]\nmax_order_notional = "1"\nmax_leverage = "1"\n\n[paper]'
    )
    with pytest.raises(RunFileError, match=r"\[paper\] replaces \[risk\]"):
        load_run(_write(tmp_path, beside), Overrides())

    other = PAPER_FILE.read_text().replace('name = "placeholder_timed"', 'name = "random_taker"')
    with pytest.raises(RunFileError, match=r"\[paper\] is only for"):
        load_run(_write(tmp_path, other), Overrides())


def test_a_bad_paper_key_is_named(tmp_path):
    text = PAPER_FILE.read_text().replace('risk_max_leverage = "2"', 'risk_max_leverage = "two"')

    with pytest.raises(RunFileError) as raised:
        load_run(_write(tmp_path, text), Overrides())

    assert raised.value.problems == ["paper: risk_max_leverage: 'two' is not a number"]


def test_parity_refuses_a_run_file_without_paper(tmp_path):
    csv = _write(tmp_path, ",".join(COLUMNS) + "\n", "paper.csv")

    with pytest.raises(SystemExit) as raised:
        main(["parity", str(RUNS / "random_taker_own.toml"), str(csv)])

    assert raised.value.code == 64


# The paper CSV -----------------------------------------------------------------------------


def _raw_run(run_id: str, config_text: str, started: str, ended: str) -> dict[str, str]:
    return {"kind": "run", "run_id": run_id, "started_at": started, "ended_at": ended, "config": config_text}


def _raw_decision(run_id: str, decided: Decided) -> dict[str, str]:
    return {
        "kind": "decision",
        "run_id": run_id,
        "decided_ns": str(decided.decided_ns),
        "intent": decided.intent,
        "side": decided.side or "",
        "risk_verdict": "approved" if decided.approved else "rejected",
        "risk_reason": decided.risk_reason or "",
    }


def _csv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "paper.csv"
    with path.open("w", newline="") as out:
        finish_export(rows, out)
    return path


def test_the_export_is_canonicalized_and_reads_back(tmp_path):
    paper = load_run(PAPER_FILE, Overrides()).run.version_config()
    # Postgres prints jsonb with spaces and its own key order; the export step makes it canonical.
    jsonb = json.dumps(dict(reversed(list(paper.items()))))
    decided = Decided(H17 + HOUR + 2_000_000_000, "enter", "buy", False, "blocked:stale_feed")
    path = _csv(
        tmp_path,
        [
            _raw_run("r1", jsonb, "2026-10-08T17:00:00.000000Z", "2026-10-08T19:00:00.000000Z"),
            _raw_decision("r1", decided),
        ],
    )

    data = read_paper_csv(path)

    (run,) = data.runs
    assert run.config_sha256 == config_sha256(paper)
    assert (run.started_ns, run.ended_ns) == (H17, H17 + 2 * HOUR)
    assert data.decisions == (PaperDecision("r1", decided),)


def test_a_tampered_hash_or_header_is_refused(tmp_path):
    path = _csv(tmp_path, [_raw_run("r1", '{"a": 1}', "2026-10-08T17:00:00Z", "2026-10-08T18:00:00Z")])
    path.write_text(path.read_text().replace(config_sha256({"a": 1}), "0" * 64))
    with pytest.raises(ParityInputError, match="config_sha256 does not match"):
        read_paper_csv(path)

    path.write_text("kind,run_id\n")
    with pytest.raises(ParityInputError, match="header must be"):
        read_paper_csv(path)


# lab parity over the fixture ---------------------------------------------------------------


@pytest.fixture(scope="module")
def fast(tmp_path_factory):
    return load_run(_write(tmp_path_factory.mktemp("fast"), FAST), Overrides())


@pytest.fixture(scope="module")
def replayed(fast, tmp_path_factory) -> list[Decided]:
    """The fixture's enter decisions, replayed once with the fast beat."""
    settings = _settings(tmp_path_factory.mktemp("reports"))
    plan = plan_run(SourceTree(FIXTURES / "own", fast.run.instrument_id), fast)
    plans = unit_plans(settings, fast, plan.segments, VariantSpec("parity", fast.run.latency_ms, True))
    units = run_units(plans, settings, run_unit)
    return [
        Decided(r.decided_ns, r.intent, r.side, r.risk_verdict == "approved", r.risk_reason)
        for u in units
        for _, r in u.decisions
        if r.intent == "enter"
    ]


def _paper(fast, decisions: list[Decided], *, config=None) -> PaperData:
    config = fast.run.version_config() if config is None else config
    run = PaperRun("r1", H17, H17 + 2 * HOUR, config, config_sha256(config))
    return PaperData(runs=(run,), decisions=tuple(PaperDecision("r1", d) for d in decisions))


def test_the_fixture_gives_the_placeholder_a_decision_to_compare(replayed):
    # 18:00 is past the 3 minute warm up and has bars from 17:55 on.
    assert any(d.decided_ns >= H17 + HOUR for d in replayed)


def test_a_csv_of_the_replays_own_decisions_scores_one(fast, replayed, tmp_path):
    out = io.StringIO()

    code = parity(_settings(tmp_path), fast, _paper(fast, replayed), out=out)

    assert code == 0
    assert out.getvalue().startswith("parity 1.0000")


def test_a_missing_paper_decision_fails_and_is_listed(fast, replayed, tmp_path):
    out = io.StringIO()

    code = parity(_settings(tmp_path), fast, _paper(fast, replayed[:-1]), out=out)

    assert code == 1
    assert "unmatched replay" in out.getvalue()


def test_a_run_with_another_config_is_left_out_and_its_keys_named(fast, tmp_path, caplog):
    other = {**fast.run.version_config(), "placeholder_stop_pct": "0.5"}
    out = io.StringIO()

    with caplog.at_level(logging.WARNING, logger="lab.parity"):
        code = parity(_settings(tmp_path), fast, _paper(fast, [], config=other), out=out)

    assert code == 2  # no hour left, so no decision to compare
    (mismatch,) = [r for r in caplog.records if r.getMessage() == "parity_config_mismatch"]
    assert mismatch.fields == {"run_id": "r1", "keys": ["placeholder_stop_pct"]}
    assert "1 runs left out" in out.getvalue()

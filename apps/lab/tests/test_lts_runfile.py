"""covers: spec 0031 AC-16 (the frozen values, native dates, every problem listed) and AC-2 (the command refuses)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.lts_runfile import load_lts_run
from lab.main import main
from lab.runfile import RunFileError
from lab.settings import LabSettings

FROZEN = Path(__file__).parents[1] / "runs" / "lts_precheck.toml"
HOLDOUT = date(2025, 10, 19)


def _edited(tmp_path, old, new):
    text = FROZEN.read_text(encoding="utf-8")
    assert old in text
    path = tmp_path / "run.toml"
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def _problems(path):
    with pytest.raises(RunFileError) as raised:
        load_lts_run(path, HOLDOUT)
    return raised.value.problems


def _refused(tmp_path, old, new, word):
    return any(word in p for p in _problems(_edited(tmp_path, old, new)))


def test_the_committed_run_file_holds_the_frozen_values():
    run = load_lts_run(FROZEN, HOLDOUT).run

    assert run.instrument_id == "PF_XBTUSD.KRAKEN"
    assert (run.lookback_s, run.sweep_buffer_ticks, run.reclaim_buffer_ticks) == (60, 1, 1)
    assert (run.invalidation_buffer_ticks, run.reclaim_timeout_ms, run.efficiency_pct_min) == (5, 3000, 90)
    assert (run.horizon_s, run.depth_band_bps) == (15, 5)
    assert (run.maker_fee, run.taker_fee, run.hurdle_k) == (Decimal("0.0002"), Decimal("0.0005"), Decimal("1.5"))
    assert (run.min_signals_per_day, run.min_positive_days) == (25, 8)
    assert run.days == [
        date(2022, 6, 1),
        date(2022, 10, 1),
        date(2023, 2, 1),
        date(2023, 6, 1),
        date(2023, 10, 1),
        date(2024, 2, 1),
        date(2024, 6, 1),
        date(2024, 10, 1),
        date(2025, 2, 1),
        date(2025, 6, 1),
        date(2025, 8, 1),
        date(2025, 10, 1),
    ]


def test_the_hash_is_of_the_file():
    assert len(load_lts_run(FROZEN, HOLDOUT).file_hash) == 64


def test_a_quoted_date_is_refused(tmp_path):
    assert _refused(tmp_path, "2022-06-01,", '"2022-06-01",', "days")


def test_a_float_fee_is_refused(tmp_path):
    assert _refused(tmp_path, 'maker_fee = "0.0002"', "maker_fee = 0.0002", "maker_fee")


def test_an_unknown_key_is_refused(tmp_path):
    assert any(
        "surprise" in p for p in _problems(_edited(tmp_path, "lookback_s = 60", "lookback_s = 60\nsurprise = 1"))
    )


def test_every_problem_is_listed(tmp_path):
    text = FROZEN.read_text(encoding="utf-8").replace("lookback_s = 60", "lookback_s = 0")
    text = text.replace("horizon_s = 15", "horizon_s = -1")
    path = tmp_path / "run.toml"
    path.write_text(text, encoding="utf-8")

    problems = _problems(path)

    assert any("lookback_s" in p for p in problems)
    assert any("horizon_s" in p for p in problems)


def test_a_holdout_day_is_refused(tmp_path):
    assert _refused(tmp_path, "2025-10-01,", "2025-10-01, 2025-11-01,", "holdout")


def test_unsorted_days_are_refused(tmp_path):
    assert any(
        "sorted" in p for p in _problems(_edited(tmp_path, "2022-06-01, 2022-10-01,", "2022-10-01, 2022-06-01,"))
    )


def test_an_unpinned_instrument_is_refused(tmp_path):
    assert _refused(tmp_path, "PF_XBTUSD.KRAKEN", "PF_NOPE.KRAKEN", "instrument_id")


def test_more_positive_days_than_days_is_refused(tmp_path):
    assert any(
        "min_positive_days" in p
        for p in _problems(_edited(tmp_path, "min_positive_days = 8", "min_positive_days = 13"))
    )


def _exit_code(argv):
    with pytest.raises(SystemExit) as raised:
        main(argv)
    return raised.value.code


def test_a_bad_run_file_exits_64(tmp_path):
    assert _exit_code(["lts-precheck", str(_edited(tmp_path, "lookback_s = 60", "lookback_s = 0"))]) == 64


def test_days_are_for_own_data_only():
    assert _exit_code(["lts-precheck", str(FROZEN), "--days", "2026-10-01"]) == 64


def test_a_true_live_flag_exits_78_before_anything_else(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    assert _exit_code(["lts-precheck", str(tmp_path / "missing.toml")]) == 78


def test_an_unreadable_live_flag_exits_78(monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "maybe")

    assert _exit_code(["lts-precheck", str(FROZEN)]) == 78


def test_trading_env_live_exits_78(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")

    assert _exit_code(["lts-precheck", str(FROZEN)]) == 78


def test_no_commit_exits_78(monkeypatch, tmp_path):
    settings = LabSettings(trading_env="demo", lab_catalog_dir=tmp_path, lab_reports_dir=tmp_path, git_commit="unknown")
    monkeypatch.setattr("lab.main.boot", lambda *a, **k: SimpleNamespace(settings=settings))

    assert _exit_code(["lts-precheck", str(FROZEN)]) == 78

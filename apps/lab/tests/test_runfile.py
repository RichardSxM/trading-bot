"""covers: spec 0008 AC-3 (the run file and overrides are validated at startup, every problem listed, exit 64)
and AC-17 (exit 78 in live, or for a replay from an image built without GIT_COMMIT)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from lab.main import main
from lab.runfile import Overrides, RunFileError, load_run
from lab.settings import LabSettings
from lab.strategies import REGISTRY

RUNS = Path(__file__).parents[1] / "runs"
GOOD = (RUNS / "random_taker_own.toml").read_text()
MAKER = (RUNS / "maker_probe.toml").read_text()


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "run.toml"
    path.write_text(text)
    return path


def test_the_committed_run_files_are_valid():
    for path in sorted(RUNS.glob("*.toml")):
        loaded = load_run(path, Overrides())
        assert loaded.run.strategy.name in REGISTRY
        assert len(loaded.sha256) == 64


def test_values_are_decimals_and_overrides_replace_keys_before_validation(tmp_path):
    loaded = load_run(_write(tmp_path, GOOD), Overrides(from_=date(2026, 10, 7), seed=11))

    assert (loaded.run.from_, loaded.run.to, loaded.run.seed) == (date(2026, 10, 7), date(2026, 10, 8), 11)
    assert loaded.run.taker_fee == Decimal("0.0005")
    assert loaded.overrides == {"from": "2026-10-07", "seed": 11}


def test_every_problem_is_listed_at_once(tmp_path):
    text = (
        GOOD.replace('maker_fee = "0.0002"', "maker_fee = 0.0002")
        .replace("latency_ms = 25", "latency_ms = -1")
        .replace('leverage = "2"\n', 'leverage = "2"\nturbo = true\n', 1)
    )

    with pytest.raises(RunFileError) as raised:
        load_run(_write(tmp_path, text), Overrides())

    problems = "\n".join(raised.value.problems)
    assert len(raised.value.problems) == 3
    assert "maker_fee" in problems
    assert "latency_ms" in problems
    assert "turbo" in problems


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (('taker_fee = "0.0005"', 'taker_fee = "1"'), "taker_fee must be in [0, 1)"),
        (("interval_s = 60", "interval_s = 60\nwarp = 3"), "warp: not a setting of random_taker"),
        (("interval_s = 60", "interval_s = 60\nseed = 3"), "seed: not a setting of random_taker"),
        (("interval_s = 60", 'interval_s = "soon"'), "strategy.config"),
        (('name = "random_taker"', 'name = "nope"'), "is not registered"),
    ],
)
def test_bad_values_are_refused(tmp_path, change, expected):
    with pytest.raises(RunFileError) as raised:
        load_run(_write(tmp_path, GOOD.replace(*change)), Overrides())

    assert expected in "\n".join(raised.value.problems)


def test_an_override_that_puts_from_after_to_is_refused(tmp_path):
    with pytest.raises(RunFileError) as raised:
        load_run(_write(tmp_path, GOOD), Overrides(from_=date(2026, 10, 9)))

    assert "after to" in raised.value.problems[0]


def _exit_code(argv: list[str]) -> int | str | None:
    with pytest.raises(SystemExit) as raised:
        main(argv)
    return raised.value.code


def test_a_bad_run_file_exits_64_before_anything_is_read(tmp_path, capsys):
    path = _write(tmp_path, GOOD.replace("seed = 7", 'seed = "seven"'))

    assert _exit_code(["replay", str(path)]) == 64
    assert "seed" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["replay"], ["replay", "x.toml", "--from", "08-10-2026"], ["nope"]])
def test_bad_arguments_exit_64(argv):
    assert _exit_code(argv) == 64


def test_pair_entry_is_refused_for_a_strategy_without_an_entry_mode(tmp_path, capsys):
    assert _exit_code(["replay", str(_write(tmp_path, GOOD)), "--pair-entry"]) == 64
    assert "no entry_mode" in capsys.readouterr().err


def test_pair_entry_is_refused_when_the_run_file_already_takes(tmp_path, capsys):
    text = MAKER.replace('entry_mode = "maker"', 'entry_mode = "taker"')

    assert _exit_code(["replay", str(_write(tmp_path, text)), "--pair-entry"]) == 64
    assert 'already sets entry_mode = "taker"' in capsys.readouterr().err


def test_a_bad_entry_mode_is_refused(tmp_path):
    with pytest.raises(RunFileError) as raised:
        load_run(_write(tmp_path, MAKER.replace('entry_mode = "maker"', 'entry_mode = "sometimes"')), Overrides())

    assert "strategy.config" in raised.value.problems[0]


def test_the_lab_refuses_live_with_exit_78(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")

    assert _exit_code(["replay", str(_write(tmp_path, GOOD))]) == 78


@pytest.mark.parametrize("commit", ["unknown", ""])
def test_a_replay_from_an_image_without_a_commit_exits_78_before_connecting(tmp_path, monkeypatch, capsys, commit):
    settings = LabSettings(trading_env="demo", git_commit=commit)
    monkeypatch.setattr("lab.main.boot", lambda *a, **k: SimpleNamespace(settings=settings, secrets={}))
    monkeypatch.setattr("lab.main.create_engine", lambda *a, **k: pytest.fail("connected to Postgres"))

    assert _exit_code(["replay", str(_write(tmp_path, GOOD))]) == 78
    assert "GIT_COMMIT" in capsys.readouterr().err


def test_context_defaults_match_the_trader_settings():
    """covers: spec 0009 AC-13 (the run file's [context] defaults are ContextSettings')."""
    from trading_core.settings import ContextSettings

    context = load_run(RUNS / "random_taker_own.toml", Overrides()).run.context
    defaults = ContextSettings(trading_env="demo")

    assert context.warmup_s == 7_200
    assert context.values("own").config_fields() == {
        "context_max_age_mark_s": defaults.context_max_age_mark_s,
        "context_max_age_index_s": defaults.context_max_age_index_s,
        "context_max_age_open_interest_s": defaults.context_max_age_open_interest_s,
        "context_max_age_funding_s": defaults.context_max_age_funding_s,
        "context_liquidation_window_s": defaults.context_liquidation_window_s,
        "context_liquidations_available": True,
    }
    assert context.values("tardis").liquidations_available is False


def test_a_context_table_sets_its_values_and_refuses_unknown_or_bad_ones(tmp_path):
    """covers: spec 0009 AC-13."""
    run = load_run(_write(tmp_path, GOOD + "\n[context]\nwarmup_s = 600\nmax_age_mark_s = 5\n"), Overrides()).run
    assert (run.context.warmup_s, run.context.max_age_mark_s, run.context.max_age_index_s) == (600, 5, 30)

    with pytest.raises(RunFileError) as exc:
        load_run(_write(tmp_path, GOOD + "\n[context]\nwarmup = 1\nmax_age_mark_s = 0\n"), Overrides())
    text = str(exc.value)
    assert "context.warmup" in text
    assert "context.max_age_mark_s" in text


def test_market_context_is_a_strategy_setting_but_its_ages_come_from_the_run():
    """covers: spec 0009 AC-11, AC-13 (use_market_context in [strategy.config]; the ages from [context])."""
    entry = REGISTRY["random_taker"]

    assert "use_market_context" in entry.user_fields()
    assert not any(name.startswith("context_") for name in entry.user_fields())


def test_the_guard_sizes_with_the_runs_taker_fee():
    """covers: spec 0011 verify step 2 (the guard's fee comes from the run, not the cached instrument)."""
    from dataclasses import replace

    from lab.strategies import _CHECK_CONTEXT
    from trading_core.node.guard import GuardedConfig

    entry = REGISTRY["random_taker"]

    config = entry.build_config({}, replace(_CHECK_CONTEXT, taker_fee=Decimal("0.001")))

    assert isinstance(config, GuardedConfig)
    assert config.taker_fee == Decimal("0.001")
    assert "taker_fee" not in entry.user_fields()

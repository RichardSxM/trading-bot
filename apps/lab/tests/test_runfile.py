"""covers: spec 0008 AC-3 (the run file and overrides are validated at startup, every problem listed, exit 64)
and AC-17 (exit 78 in live)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from lab.main import main
from lab.runfile import Overrides, RunFileError, load_run
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

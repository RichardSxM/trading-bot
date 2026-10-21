"""covers: spec 0015 AC-2 (replay study file, grid cap 4, `lab replay` refuses a grid), AC-3 (one `replay()`
per grid point) and AC-24 (replay days: development list, folds minimum, dropped and missing days)."""

from datetime import date
from pathlib import Path
from typing import cast

import pytest

from lab import study as study_module
from lab.runfile import LoadedRun, Overrides, RunFileError, load_run
from lab.settings import LabSettings
from lab.strategies import registry_entry
from lab.study import EXIT_FAILED, EXIT_OK, LoadedStudy, load_study
from trading_core.catalog.days import write_gaps
from trading_core.catalog.layout import CatalogLayout
from trading_core.lab.errors import MissingDaysError
from trading_core.lab.segments import day_start_ns
from trading_core.lab.study_days import firsts_of_month
from trading_core.lab.variants import BASE
from trading_core.marketdata.gaps import Gap
from trading_core.marketdata.hours import DAY_NS
from trading_core.recording.enums import StudyRole
from trading_core.recording.rows import JsonValue, StudyRow

from .conftest import INSTRUMENT

RUNS = Path(__file__).resolve().parents[1] / "runs"

STUDY = """
source = "tardis"
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2022-04-01
to = 2025-10-18
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
latency_ms = 25
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "2000"
max_leverage = "2"

[strategy]
name = "random_taker"
[strategy.config]
interval_s = 60
hold_s = 30
notional_usd = "1000"

[study]
name = "NAME"
lab = "replay"
entry_mode = "taker"
seed = 42
"""


def _write(tmp_path: Path, text: str, name: str = "s") -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text.replace("NAME", "replay-check"))
    return path


def _tardis(root: Path, days: list[date]) -> Path:
    """A Tardis tree with an (empty) manifest for each day; the hours themselves are not needed to plan."""
    manifests = root / "tardis" / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    for day in days:
        (manifests / f"{day.isoformat()}.json").write_text(f'{{"date": "{day}", "gaps": [], "streams": []}}')
    return root


def _gap_whole_day(root: Path, day: date) -> None:
    start = day_start_ns(day)
    write_gaps(CatalogLayout(root / "tardis"), [Gap(start, INSTRUMENT, "reconnect", start + DAY_NS - 1)])


DEVELOPMENT = firsts_of_month(date(2022, 4, 1), date(2025, 10, 18))


def _days(study: LoadedStudy) -> dict[str, JsonValue]:
    days = study.plan["days"]
    assert isinstance(days, dict)
    return days


def test_a_replay_study_plans_43_development_days_and_four_folds(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    study = load_study(_write(tmp_path, STUDY), catalog_dir=catalog)

    days = _days(study)
    assert len(cast(list[str], days["development"])) == 43
    assert cast(list[str], days["development"])[0] == "2022-04-01"
    assert cast(list[str], days["development"])[-1] == "2025-10-01"
    assert days["dropped"] == []
    assert study.plan["ranges"] == {"development": ["2022-04-01", "2025-10-18"], "holdout_start": "2025-10-19"}
    assert study.plan["folds"] == {"first_in_sample_days": 15, "step_days": 7}
    # 15 in sample, then (43 - 15) // 7 = 4 whole steps.
    assert (43 - 15) // 7 == 4
    assert study.plan["grid"] == [{}]
    assert study.points == ((BASE, {}),)
    # The run's `to` is the day before the holdout; the plan keeps the file's own `to`.
    assert study.loaded.run.to == date(2025, 10, 18)
    assert cast(dict[str, JsonValue], study.plan["run_file"])["to"] == "2025-10-18"


def test_the_day_list_is_part_of_the_plan_hash(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    full = load_study(_write(tmp_path, STUDY), catalog_dir=catalog).plan_hash
    later = load_study(
        _write(tmp_path, STUDY.replace("from = 2022-04-01", "from = 2022-05-01"), "l"), catalog_dir=catalog
    )

    assert later.plan_hash != full


def test_22_days_are_enough_and_21_are_not(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    study = load_study(_write(tmp_path, STUDY.replace("from = 2022-04-01", "from = 2024-01-01")), catalog_dir=catalog)
    assert len(cast(list[str], _days(study)["development"])) == 22

    with pytest.raises(RunFileError, match="21 development days"):
        load_study(_write(tmp_path, STUDY.replace("from = 2022-04-01", "from = 2024-02-01")), catalog_dir=catalog)


def test_a_day_gapped_in_every_hour_is_dropped_and_counts_against_the_minimum(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    _gap_whole_day(catalog, date(2023, 1, 1))
    study = load_study(_write(tmp_path, STUDY), catalog_dir=catalog)
    assert _days(study)["dropped"] == ["2023-01-01"]
    assert study.dropped == (date(2023, 1, 1),)

    _gap_whole_day(catalog, date(2024, 3, 1))
    with pytest.raises(RunFileError, match="gaps in every hour"):
        load_study(_write(tmp_path, STUDY.replace("from = 2022-04-01", "from = 2024-01-01")), catalog_dir=catalog)


def test_a_missing_manifest_raises_with_every_missing_day(tmp_path):
    gone = {date(2023, 6, 1), date(2024, 2, 1)}
    catalog = _tardis(tmp_path / "catalog", [d for d in DEVELOPMENT if d not in gone])

    with pytest.raises(MissingDaysError) as caught:
        load_study(_write(tmp_path, STUDY), catalog_dir=catalog)
    assert caught.value.days == sorted(gone)


def test_missing_days_exit_2_and_print_the_import_command(capsys):
    assert study_module.missing_days_exit("study", [date(2023, 6, 1), date(2024, 2, 1)]) == 2
    err = capsys.readouterr().err
    assert "2023-06-01" in err
    assert "2024-02-01" in err
    assert "lab import-tardis 2023-06-01 2024-02-01" in err


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda t: t.replace("from = 2022-04-01", "from = 2022-04-02"), "not a 1st of the month"),
        (lambda t: t.replace("from = 2022-04-01", "from = 2022-03-01"), "before 2022-04-01"),
        (lambda t: t.replace('source = "tardis"', 'source = "own"'), "free Tardis days only"),
        (lambda t: t.replace("to = 2025-10-18", "to = 2025-10-19"), "reaches into the holdout"),
        (lambda t: t.replace('entry_mode = "taker"\nseed', 'entry_mode = "maker"\nseed'), 'only "taker"'),
        (lambda t: t.replace('entry_mode = "taker"\n', ""), "needs entry_mode"),
        (lambda t: t + "\n[strategy.grid]\ninterval_s = [10, 20, 30, 40, 50]\n", "at most 4"),
    ],
)
def test_a_bad_replay_study_is_refused(tmp_path, change, message):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    with pytest.raises(RunFileError, match=message):
        load_study(_write(tmp_path, _with_grid_table(change(STUDY))), catalog_dir=catalog)


def _with_grid_table(text: str) -> str:
    # A [strategy.grid] appended after [study] has to sit inside [strategy] for TOML; move [study] last.
    if "[strategy.grid]" not in text:
        return text
    head, study = text.split("[study]")
    body, grid = study.split("[strategy.grid]")
    return f"{head}[strategy.grid]{grid}\n[study]{body}"


def test_entry_mode_belongs_to_the_study_table_only(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    maker = (
        STUDY.replace('name = "random_taker"', 'name = "maker_probe"')
        .replace('notional_usd = "1000"', 'notional_usd = "1000"\nentry_mode = "maker"\nentry_timeout_s = 10')
        .replace('entry_mode = "taker"\nseed', 'entry_mode = "maker"\nseed')
    )
    with pytest.raises(RunFileError, match=r"strategy\.config: entry_mode is set by"):
        load_study(_write(tmp_path, maker), catalog_dir=catalog)

    fine = load_study(
        _write(tmp_path, maker.replace('entry_mode = "maker"\nentry_timeout_s', "entry_timeout_s")), catalog_dir=catalog
    )
    assert fine.section.entry_mode == "maker"


def test_a_replay_grid_expands_in_sorted_key_order(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    text = _with_grid_table(STUDY.replace("interval_s = 60\n", "") + "\n[strategy.grid]\ninterval_s = [60, 120]\n")
    study = load_study(_write(tmp_path, text), catalog_dir=catalog)

    assert study.points == (("grid-1", {"interval_s": 60}), ("grid-2", {"interval_s": 120}))
    assert study.plan["grid"] == [{"interval_s": 60}, {"interval_s": 120}]


def test_lab_replay_refuses_a_run_file_with_a_grid(tmp_path):
    text = (RUNS / "random_taker.toml").read_text().replace(
        "interval_s = 60\n", ""
    ) + "[strategy.grid]\ninterval_s = [60, 120]\n"
    path = tmp_path / "grid.toml"
    path.write_text(text)

    with pytest.raises(RunFileError, match="a grid runs only in `lab study`"):
        load_run(path, Overrides())


def test_each_grid_point_runs_as_its_own_replay_with_one_base_variant(tmp_path, monkeypatch):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    text = _with_grid_table(STUDY.replace("interval_s = 60\n", "") + "\n[strategy.grid]\ninterval_s = [60, 120]\n")
    study = load_study(_write(tmp_path, text), catalog_dir=catalog)
    calls = []

    def fake_replay(settings, loaded, store, *, variants, study, dropped_days):
        calls.append((dict(loaded.run.strategy.config), loaded.run.strategy.grid, list(variants), dropped_days))
        return EXIT_FAILED if loaded.run.strategy.config["interval_s"] == 60 else EXIT_OK

    monkeypatch.setattr(study_module, "replay", fake_replay)
    entry = registry_entry("random_taker")
    assert isinstance(study.loaded, LoadedRun)
    code = study_module._replay_points(
        cast(LabSettings, None),
        study.loaded,
        cast(StudyRow, None),
        cast(study_module.StudyStore, None),
        entry,
        "taker",
        [(0, {"interval_s": 60}), (1, {"interval_s": 120})],
        StudyRole.DEVELOPMENT,
        (date(2023, 1, 1),),
    )

    # The first point failed, the second still ran, and the exit is 1.
    assert code == EXIT_FAILED
    assert [c[0]["interval_s"] for c in calls] == [60, 120]
    assert all(c[1] == {} for c in calls)
    assert all(len(c[2]) == 1 and c[2][0].name == BASE for c in calls)
    # random_taker has no entry_mode setting, so the variant's entry mode stays unset.
    assert all(c[2][0].entry_mode is None for c in calls)
    assert all(c[3] == (date(2023, 1, 1),) for c in calls)


def test_the_committed_replay_study_file_is_valid(tmp_path):
    catalog = _tardis(tmp_path / "catalog", DEVELOPMENT)
    study = load_study(Path(__file__).resolve().parents[1] / "studies" / "random_taker.toml", catalog_dir=catalog)

    assert study.section.name == "random_taker"
    assert study.section.entry_mode == "taker"
    assert len(cast(list[str], _days(study)["development"])) == 43

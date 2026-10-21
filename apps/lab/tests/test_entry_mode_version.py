"""covers: spec 0015 AC-25 (the effective entry mode joins the version config: one version per entry mode
in a batch, taker and maker studies get different frozen hashes, other strategies and parity unchanged)."""

from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid7

import pytest

from lab import replay as replay_module
from lab import study as study_module
from lab.replay import effective_entry_mode, replay
from lab.runfile import LoadedRun, Overrides, load_run
from lab.strategies import registry_entry
from lab.study import load_study
from trading_core.lab.variants import BASE, VariantSpec, expand_variants

from .conftest import FIXTURES, lab_settings
from .test_study_replay import DEVELOPMENT, STUDY, _tardis, _write

RUNS = Path(__file__).parents[1] / "runs"


class _PrepareOnly:
    """Records each ``prepare`` and hands out a fresh id; the batch itself is stubbed out."""

    def __init__(self) -> None:
        self.prepared: list[dict[str, object]] = []

    def prepare(self, instrument, name, kind, git_commit, config, frozen) -> UUID:
        self.prepared.append(dict(config))
        return uuid7()

    def write_bundle(self, bundle):
        raise AssertionError("no bundle is written here")


def _own(path: Path) -> LoadedRun:
    """A committed run file pointed at the fixture's own recorder hours."""
    loaded = load_run(path, Overrides())
    base = load_run(RUNS / "random_taker_own.toml", Overrides())
    run = loaded.run.model_copy(update={"source": "own", "from_": base.run.from_, "to": base.run.to})
    return replace(loaded, run=run)


def _replay(monkeypatch, tmp_path, loaded: LoadedRun, variants: list[VariantSpec]):
    store = _PrepareOnly()
    seen: dict[str, dict[str, UUID]] = {}

    def fake_batch(batch, variants, store, *, target):
        seen["version_ids"] = dict(batch.version_ids)
        return [], False

    monkeypatch.setattr(replay_module, "run_batch", fake_batch)
    code = replay(lab_settings(FIXTURES, tmp_path / "reports"), loaded, store, variants=variants)  # type: ignore[arg-type]
    assert code == 0
    return store.prepared, seen["version_ids"]


def test_a_pair_entry_batch_records_one_version_per_entry_mode(monkeypatch, tmp_path):
    loaded = _own(RUNS / "maker_probe.toml")
    prepared, version_ids = _replay(monkeypatch, tmp_path, loaded, expand_variants(25, "maker", pair_entry=True))

    assert [c["entry_mode"] for c in prepared] == ["maker", "taker"]
    assert version_ids[BASE] != version_ids["entry=taker"]
    # maker_probe.toml already says entry_mode = "maker", so the base version's config is the file's.
    assert prepared[0] == dict(loaded.run.strategy.config)


def test_variants_that_share_an_entry_mode_share_a_version(monkeypatch, tmp_path):
    loaded = _own(RUNS / "maker_probe.toml")
    prepared, version_ids = _replay(monkeypatch, tmp_path, loaded, expand_variants(25, "maker", sweep_latency=True))

    assert len(prepared) == 1
    assert len(set(version_ids.values())) == 1


def test_a_strategy_without_an_entry_mode_keeps_its_version_config(monkeypatch, tmp_path):
    loaded = _own(RUNS / "random_taker_own.toml")
    prepared, _ = _replay(monkeypatch, tmp_path, loaded, expand_variants(25, None, queue_bound=True))

    assert prepared == [dict(loaded.run.strategy.config)]
    assert "entry_mode" not in prepared[0]


def test_the_default_entry_mode_is_recorded_when_the_file_leaves_it_out(tmp_path):
    loaded = _own(RUNS / "maker_probe.toml")
    config = {k: v for k, v in loaded.run.strategy.config.items() if k != "entry_mode"}
    bare = replace(
        loaded,
        run=loaded.run.model_copy(update={"strategy": loaded.run.strategy.model_copy(update={"config": config})}),
    )
    entry = registry_entry("maker_probe")

    mode = effective_entry_mode(bare, entry, VariantSpec(BASE, 25, True))

    assert mode == "maker"
    assert bare.run.version_config(mode)["entry_mode"] == "maker"


def test_a_paper_run_never_takes_an_entry_mode():
    loaded = load_run(RUNS / "placeholder_paper.toml", Overrides())
    entry = registry_entry(loaded.run.strategy.name)

    assert effective_entry_mode(loaded, entry, VariantSpec(BASE, 25, True, "taker")) is None
    # Parity records version_config() with no argument: the trader's stored config, unchanged.
    assert loaded.run.version_config() == loaded.run.version_config("taker") == dict(loaded.run.paper or {})


@pytest.fixture
def catalog(tmp_path):
    return _tardis(tmp_path / "catalog", DEVELOPMENT)


def _maker_study(tmp_path, catalog, mode: str):
    text = (
        STUDY.replace('name = "random_taker"', 'name = "maker_probe"')
        .replace('entry_mode = "taker"', f'entry_mode = "{mode}"')
        .replace('notional_usd = "1000"', 'notional_usd = "1000"\nentry_timeout_s = 10')
    )
    return load_study(_write(tmp_path, text, mode), catalog_dir=catalog)


def test_a_taker_and_a_maker_study_of_one_strategy_get_different_frozen_hashes(tmp_path, catalog):
    entry = registry_entry("maker_probe")
    hashes = {}
    for mode in ("taker", "maker"):
        study = _maker_study(tmp_path, catalog, mode)
        assert isinstance(study.loaded, LoadedRun)
        (_, point), *_ = study.points
        config = study_module._version_config(entry, {**study.loaded.run.strategy.config, **point}, mode)
        frozen = entry.frozen(config)
        assert frozen is not None
        # The hash the study records is the one replay() records for the point's runs.
        variant = VariantSpec(BASE, 25, True, mode)
        loaded = study_module._with_point(study.loaded, point)
        replayed = loaded.run.version_config(effective_entry_mode(loaded, entry, variant))
        assert entry.frozen(replayed) == frozen
        hashes[mode] = frozen.frozen_hash
    # Different hashes, so one study's spent holdout never blocks the other's.
    assert hashes["taker"] != hashes["maker"]


def test_a_random_taker_study_hash_is_unchanged(tmp_path, catalog):
    study = load_study(_write(tmp_path, STUDY), catalog_dir=catalog)
    entry = registry_entry("random_taker")
    assert isinstance(study.loaded, LoadedRun)
    (_, point), *_ = study.points
    config = {**study.loaded.run.strategy.config, **point}

    assert study_module._version_config(entry, config, "taker") == config

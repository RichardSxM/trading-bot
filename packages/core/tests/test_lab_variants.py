"""covers: spec 0008 AC-6 (variant expansion: one change per flag, plan order, duplicates dropped)."""

import pytest

from trading_core.lab.variants import VariantError, VariantSpec, expand_variants


def _names(variants: list[VariantSpec]) -> list[str]:
    return [v.name for v in variants]


def test_no_flag_runs_base_alone():
    (base,) = expand_variants(25, None)

    assert base == VariantSpec("base", 25, True, None)


def test_every_flag_adds_its_own_variants_in_plan_order():
    variants = expand_variants(25, "maker", queue_bound=True, sweep_latency=True, pair_entry=True)

    assert _names(variants) == ["base", "queue=optimistic", "latency=0", "latency=4x", "entry=taker"]
    by_name = {v.name: v for v in variants}
    # Each variant changes exactly one thing against base.
    assert by_name["queue=optimistic"] == VariantSpec("queue=optimistic", 25, False, "maker")
    assert by_name["latency=0"] == VariantSpec("latency=0", 0, True, "maker")
    assert by_name["latency=4x"] == VariantSpec("latency=4x", 100, True, "maker")
    assert by_name["entry=taker"] == VariantSpec("entry=taker", 25, True, "taker")


def test_a_variant_equal_to_base_is_never_run():
    assert _names(expand_variants(0, None, sweep_latency=True)) == ["base"]


def test_file_names_use_a_dash():
    assert [v.file_name for v in expand_variants(25, None, sweep_latency=True)] == ["base", "latency-0", "latency-4x"]


def test_pair_entry_needs_an_entry_mode():
    with pytest.raises(VariantError, match="no entry_mode"):
        expand_variants(25, None, pair_entry=True)


def test_pair_entry_is_refused_when_base_already_takes():
    with pytest.raises(VariantError, match="already sets"):
        expand_variants(25, "taker", pair_entry=True)


def test_a_negative_latency_is_refused():
    with pytest.raises(ValueError, match="latency_ms"):
        expand_variants(-1, None)

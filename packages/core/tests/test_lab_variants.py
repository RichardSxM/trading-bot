"""covers: spec 0008 AC-6 (variant expansion: one change per flag, plan order, duplicates dropped), spec 0014 AC-7."""

from decimal import Decimal

import pytest

from trading_core.lab.variants import (
    CandleVariantSpec,
    VariantError,
    VariantSpec,
    expand_candle_variants,
    expand_variants,
)


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


# covers: spec 0014 AC-7 (candle variants: base, the slippage sweep, the grid)


def test_a_candle_batch_without_flags_runs_base_alone():
    assert expand_candle_variants(Decimal(2), {}) == [CandleVariantSpec("base", Decimal(2))]


def test_the_slippage_sweep_adds_zero_and_twice_after_base():
    variants = expand_candle_variants(Decimal("2.5"), {}, sweep_slippage=True)

    assert [(v.name, v.slippage_bps) for v in variants] == [
        ("base", Decimal("2.5")),
        ("slippage=0", Decimal(0)),
        ("slippage=2x", Decimal(5)),
    ]


def test_the_slippage_sweep_adds_nothing_at_zero_slippage():
    assert [v.name for v in expand_candle_variants(Decimal(0), {}, sweep_slippage=True)] == ["base"]


def test_a_grid_replaces_base_with_its_cross_product_in_sorted_key_and_file_order():
    variants = expand_candle_variants(Decimal(2), {"slow": [26, 21], "fast": [8, 12]})

    assert [(v.name, dict(v.grid_point)) for v in variants] == [
        ("grid-1", {"fast": 8, "slow": 26}),
        ("grid-2", {"fast": 8, "slow": 21}),
        ("grid-3", {"fast": 12, "slow": 26}),
        ("grid-4", {"fast": 12, "slow": 21}),
    ]
    assert {v.slippage_bps for v in variants} == {Decimal(2)}


def test_a_grid_allows_sixteen_points_and_refuses_more():
    assert len(expand_candle_variants(Decimal(2), {"a": [1, 2, 3, 4], "b": [1, 2, 3, 4]})) == 16
    with pytest.raises(VariantError, match="17 variants"):
        expand_candle_variants(Decimal(2), {"a": list(range(17))})


def test_a_grid_refuses_the_sweep_and_empty_lists():
    with pytest.raises(VariantError, match="--sweep-slippage"):
        expand_candle_variants(Decimal(2), {"fast": [8]}, sweep_slippage=True)
    with pytest.raises(VariantError, match="must not be empty: slow"):
        expand_candle_variants(Decimal(2), {"fast": [8], "slow": []})

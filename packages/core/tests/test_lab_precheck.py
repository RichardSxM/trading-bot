"""covers: spec 0033 AC-12, AC-12a, the pre check pass rules: reference mean, main strictly above, neighbours not
below, the tranche offset spread, AC-12a per hour baselines, the control deciding nothing, and missing Sharpe.
"""

from decimal import Decimal

import pytest

from trading_core.lab.precheck import RunFigures, precheck_verdicts, reference


def fig(name, family="ewma", role="main", hour=0, h1="1", h2="1", full="1"):
    d = lambda v: None if v is None else Decimal(v)  # noqa: E731
    return RunFigures(name, family, role, hour, d(h1), d(h2), d(full))


BASE = {
    0: fig("base@00", "", "", 0, "1.0", "0.4", "0.7"),
    6: fig("base@06", "", "", 6, "0.8", "0.6", "0.7"),
    12: fig("base@12", "", "", 12, "0.6", "0.8", "0.7"),
    18: fig("base@18", "", "", 18, "0.2", "0.6", "0.3"),
}


def verdict(runs):
    return {v.family: v for v in precheck_verdicts(BASE, runs)}


def test_reference_is_the_mean_per_half():
    assert reference(BASE) == (Decimal("0.65"), Decimal("0.6"))
    assert reference({0: fig("b", h1=None)}) is None


def test_ewma_main_beats_and_neighbours_not_below():
    runs = [fig("ewma20", h1="0.7", h2="0.61"), fig("ewma10", role="neighbour", h1="0.65", h2="0.6")]
    assert verdict(runs)["ewma"].passed
    runs[1] = fig("ewma10", role="neighbour", h1="0.64", h2="0.9")
    assert not verdict(runs)["ewma"].passed


def test_main_equal_to_the_reference_fails():
    assert not verdict([fig("ewma20", h1="0.65", h2="0.7")])["ewma"].passed


def test_tranche_spread_must_be_under_half_the_baseline():
    # Baseline full Sharpe spread 0.7 - 0.3 = 0.4, half is 0.2.
    main = fig("tranche4", "tranche", h1="0.7", h2="0.7", full="0.8")
    good = [main, fig("t4_01", "tranche", "offset", 1, full="0.7"), fig("t4_02", "tranche", "offset", 2, full="0.75")]
    assert verdict(good)["tranche"].passed
    bad = [*good, fig("t4_03", "tranche", "offset", 3, full="0.55")]
    assert not verdict(bad)["tranche"].passed


def test_scale_mains_beat_their_own_hour_and_neighbours_the_00_baseline():
    mains = [
        fig("s@00", "scale", "main", 0, "1.1", "0.5"),
        fig("s@06", "scale", "main", 6, "0.9", "0.7"),
        fig("s@12", "scale", "main", 12, "0.7", "0.9"),
        fig("s@18", "scale", "main", 18, "0.3", "0.7"),
    ]
    runs = [*mains, fig("n", "scale", "neighbour", 0, "1.0", "0.4"), fig("c", "scale", "control", 0, "-5", "-5")]
    result = verdict(runs)["scale"]
    assert result.passed
    assert any("control" in r for r in result.reasons)
    runs[3] = fig("s@18", "scale", "main", 18, "0.3", "0.6")  # equal to its baseline in H2
    assert not verdict(runs)["scale"].passed


def test_missing_sharpe_fails_and_bad_input_raises():
    assert not verdict([fig("ewma20", h1=None)])["ewma"].passed
    with pytest.raises(ValueError, match="unknown"):
        precheck_verdicts(BASE, [fig("x", "turtle")])
    with pytest.raises(ValueError, match="exactly one"):
        precheck_verdicts(BASE, [fig("a"), fig("b")])

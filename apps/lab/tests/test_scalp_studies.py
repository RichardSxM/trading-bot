"""covers: spec 0017 AC-3 (each replay study's max_spread_bps is the audit p95 rounded up), AC-10 (the candidate
study files exist exactly as the children list them, with the shared replay and screen fields), AC-21 (the
``bollinger_rsi_r1`` revision: the v4 screen with maker exits on the candle gate path)."""

import tomllib
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest

from lab.study import load_study
from trading_core.lab.study_days import firsts_of_month

D = Decimal
STUDIES = Path(__file__).resolve().parents[1] / "studies"
AUDIT_P95 = D("6.2615")  # audit-20261021, recorded in verify.md (AC-3)

REPLAY = {"bollinger_rsi": "25", "vwap_retest": None, "delta_divergence": "25"}  # study → regime_adx_max
SCREENS = {
    "bollinger_rsi_screen_v4": ("bollinger_rsi", None),
    "bollinger_rsi_screen_adx_v2": ("bollinger_rsi", "25"),
    "vwap_retest_screen": ("vwap_retest", None),
    "vwap_retest_screen_adx": ("vwap_retest", "25"),
}
SCREEN_OF = {"bollinger_rsi": "bollinger_rsi_screen_v4", "vwap_retest": "vwap_retest_screen"}
SHARED = {
    "maker_fee": "0.0002",
    "taker_fee": "0.0005",
    "starting_balance_usd": "10000",
    "leverage": "2",
    "seed": 7,
}
RISK = {"max_order_notional": "2000", "max_leverage": "2"}


def _raw(name):
    with (STUDIES / f"{name}.toml").open("rb") as f:
        return tomllib.load(f)


def _catalog(root):
    """An empty manifest for every replay development day, so loading needs no recorded data (CI has none)."""
    manifests = root / "tardis" / "manifests"
    manifests.mkdir(parents=True)
    for day in firsts_of_month(date(2022, 4, 1), date(2025, 10, 18)):
        (manifests / f"{day.isoformat()}.json").write_text(f'{{"date": "{day}", "gaps": [], "streams": []}}')
    return root


def _common(raw, name, tmp_path):
    assert {k: raw[k] for k in SHARED} == SHARED
    assert raw["risk"] == RISK  # risk_per_trade left at its default
    assert raw["study"]["name"] == name
    assert raw["study"]["seed"] == 42
    assert len(load_study(STUDIES / f"{name}.toml", catalog_dir=_catalog(tmp_path / "catalog")).points) == 4


def test_every_spec_0017_study_file_exists_and_no_others_were_added():
    # covers: AC-10 (book_imbalance.toml is absent on purpose: its pre check failed, task 20 skipped)
    names = {*REPLAY, *SCREENS, "bollinger_rsi_r1"}
    assert {n for n in names if (STUDIES / f"{n}.toml").is_file()} == names
    assert not (STUDIES / "book_imbalance.toml").exists()


@pytest.mark.parametrize("name", sorted(REPLAY))
def test_a_replay_study_has_the_shared_replay_fields(name, tmp_path):
    # covers: AC-10
    raw = _raw(name)
    _common(raw, name, tmp_path)
    assert raw["source"] == "tardis"
    assert (raw["from"], raw["to"]) == (date(2022, 4, 1), date(2025, 10, 18))
    assert raw["latency_ms"] == 25
    assert raw["study"]["lab"] == "replay"
    assert raw["study"]["entry_mode"] == "maker"
    assert raw["strategy"]["name"] == name
    assert raw["strategy"]["config"].get("regime_adx_max") == REPLAY[name]


@pytest.mark.parametrize("name", sorted(REPLAY))
def test_a_replay_study_caps_the_spread_at_the_audit_p95_rounded_up(name):
    # covers: AC-3
    cap = D(_raw(name)["strategy"]["config"]["max_spread_bps"])
    assert cap == AUDIT_P95.quantize(D("0.01"), rounding=ROUND_CEILING) == D("6.27")


@pytest.mark.parametrize("name", sorted(SCREENS))
def test_a_candle_screen_has_the_screen_fields_and_the_replay_rules(name, tmp_path):
    # covers: AC-10
    strategy, regime = SCREENS[name]
    raw = _raw(name)
    _common(raw, name, tmp_path)
    assert (raw["from"], raw["to"]) == (date(2023, 1, 1), date(2025, 10, 18))
    assert raw["slippage_bps"] == "2"
    assert raw["study"]["lab"] == "candle"
    assert "candle_gate" not in raw["study"]  # a screen is never a gate study
    config = raw["strategy"]["config"]
    assert config["entry_mode"] == "maker"
    assert "max_spread_bps" not in config  # the candle lab's spread is synthetic
    assert config.get("regime_adx_max") == regime

    replay = _raw(strategy)["strategy"]
    drop = {"max_spread_bps", "regime_adx_max", "entry_mode"}
    assert raw["strategy"]["name"] == strategy
    assert {k: v for k, v in config.items() if k not in drop} == {
        k: v for k, v in replay["config"].items() if k not in drop
    }
    assert raw["strategy"]["grid"] == replay["grid"]


def test_bollinger_rsi_r1_is_the_v4_screen_with_maker_exits_on_the_candle_gate_path(tmp_path):
    # covers: AC-21, AC-19 (min_target_bps 6 for a maker exit study), AC-20 (candle_gate in the study file)
    raw = _raw("bollinger_rsi_r1")
    _common(raw, "bollinger_rsi_r1", tmp_path)
    v4 = _raw(SCREEN_OF["bollinger_rsi"])
    assert raw["study"]["lab"] == "candle"
    assert raw["study"]["candle_gate"] is True
    assert raw["strategy"]["name"] == "bollinger_rsi"
    assert raw["strategy"]["config"] == {**v4["strategy"]["config"], "exit_mode": "maker", "min_target_bps": "6"}
    assert raw["strategy"]["grid"] == v4["strategy"]["grid"]
    assert {k: raw[k] for k in ("from", "to", "slippage_bps", "stale_quote_s")} == {
        k: v4[k] for k in ("from", "to", "slippage_bps", "stale_quote_s")
    }

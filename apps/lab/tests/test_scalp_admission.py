"""covers: spec 0017 AC-7 (the scalping registry entries and their run fields per lab), AC-8 (a candle screen
is admitted to the candle lab only with ``candle_screen``, and never holds out), AC-9 (``lab screen``), and
AC-20 (the candle gate path: its refusals, its plan, its floor, and its holdout)."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from lab.runfile import Overrides, RunFileError, load_candle_run
from lab.screen import EXIT_FAIL, EXIT_PASS, run_screen
from lab.strategies import _CHECK_CONTEXT, registry_entry
from lab.study import EXIT_OK, EXIT_USAGE, StudyStore, load_study, run_holdout, run_study
from trading_core.node.bollinger_rsi import BollingerRsiConfig
from trading_core.recording.enums import StrategyKind
from trading_core.secrets import POSTGRES_PASSWORD, read_secret
from trading_core.settings import Settings

from .conftest import lab_settings
from .test_study import TODAY, _catalog

CANDLE_RUN = """
instrument_id = "PF_XBTUSD.KRAKEN"
from = 2025-09-01
to = 2025-09-05
seed = 7
maker_fee = "0.0002"
taker_fee = "0.0005"
slippage_bps = "2"
starting_balance_usd = "10000"
leverage = "2"

[risk]
max_order_notional = "2000"
max_leverage = "2"

[strategy]
name = "STRATEGY"
[strategy.config]
entry_mode = "maker"
"""


def _write(tmp_path: Path, text: str, name: str = "run") -> Path:
    path = tmp_path / f"{name}.toml"
    path.write_text(text)
    return path


def test_bollinger_rsi_is_a_maker_scalping_candidate_with_a_candle_screen():
    entry = registry_entry("bollinger_rsi")

    assert (entry.kind, entry.default_entry_mode, entry.candle_screen) == (StrategyKind.SCALPING, "maker", True)
    assert entry.module == "trading_core.node.bollinger_rsi"
    assert {"bb_k", "rsi_low", "max_spread_bps", "min_target_bps", "entry_mode", "time_stop_min"} <= entry.user_fields()
    assert not {"env", "run_id", "instrument_id", "taker_fee"} & entry.user_fields()
    assert registry_entry("maker_probe").candle_screen is False


def test_a_candle_run_gives_the_candle_stale_fields_and_a_replay_run_does_not():
    entry = registry_entry("bollinger_rsi")
    candle = replace(_CHECK_CONTEXT, lab="candle", stale_quote_s=Decimal(120), stale_check_s=60)

    in_candle = entry.build_config({}, candle)
    in_replay = entry.build_config({}, _CHECK_CONTEXT)

    assert isinstance(in_candle, BollingerRsiConfig)
    assert isinstance(in_replay, BollingerRsiConfig)
    assert (in_candle.stale_quote_s, in_candle.stale_check_s) == (Decimal(120), 60)
    assert "stale_quote_s" not in entry.run_fields(_CHECK_CONTEXT)
    assert (in_replay.stale_quote_s, in_replay.stale_check_s) == (Decimal(300), 60)  # the config's defaults


def test_the_candle_lab_admits_a_candle_screen_and_refuses_other_scalping_strategies(tmp_path):
    loaded = load_candle_run(_write(tmp_path, CANDLE_RUN.replace("STRATEGY", "bollinger_rsi")), Overrides())
    assert loaded.bar_spec == "1-MINUTE-LAST"

    with pytest.raises(RunFileError, match="scalping candidates with a candle screen"):
        load_candle_run(_write(tmp_path, CANDLE_RUN.replace("STRATEGY", "maker_probe"), "probe"), Overrides())


SCREEN = (
    CANDLE_RUN.replace("STRATEGY", "bollinger_rsi").replace("to = 2025-09-05", "to = 2025-09-03")
    + """[strategy.grid]
bb_k = ["2.0"]

[study]
name = "NAME"
lab = "candle"
seed = 42
"""
)


@pytest.mark.postgres
def test_a_candle_screen_runs_its_development_but_never_holds_out(tmp_path, capsys):
    """Runs in the lab service: ``docker compose --profile lab run --rm -T --entrypoint pytest lab -q -m postgres``."""
    catalog, reports = tmp_path / "catalog", tmp_path / "reports"
    _catalog(catalog)  # the plan runs development up to the day before holdout_start
    settings = lab_settings(catalog, reports).model_copy(update={"git_commit": uuid4().hex + uuid4().hex[:8]})
    name = f"screen-{uuid4().hex[:8]}"
    base = Settings(trading_env="demo", postgres_user="lab_app")
    store = StudyStore(create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=2))
    try:
        # A fresh config each run: this database persists.
        text = SCREEN.replace("NAME", name).replace(
            'entry_mode = "maker"', f'entry_mode = "maker"\nrsi_low = {20 + uuid4().int % 9}'
        )
        assert run_screen(settings, name, store.engine) == EXIT_USAGE  # no development run yet
        assert run_study(settings, load_study(_write(tmp_path, text, "screen")), store) == EXIT_OK

        # A random walk may pass or fail; either way the screen writes its report on the final point.
        code = run_screen(settings, name, store.engine)
        assert code in (EXIT_PASS, EXIT_FAIL)
        report = json.loads((reports / f"screen-{name}" / "screen.json").read_text())
        assert (report["study"], report["strategy"], report["final_grid_point"]) == (name, "bollinger_rsi", 0)
        assert report["verdict"] == ("pass" if code == EXIT_PASS else "fail")
        assert report["figures"]["min_trades_per_day"] == 17
        assert run_holdout(settings, name, store, today=TODAY) == EXIT_USAGE
        assert "a candle screen never holds out" in capsys.readouterr().err
    finally:
        store.engine.dispose()


def test_only_bollinger_rsi_may_take_the_candle_gate_path():
    assert registry_entry("bollinger_rsi").candle_gate is True
    assert registry_entry("vwap_retest").candle_gate is False
    assert registry_entry("delta_divergence").candle_gate is False


def test_candle_gate_is_in_the_plan_only_when_the_study_file_sets_it(tmp_path):
    plain = load_study(_write(tmp_path, SCREEN.replace("NAME", "plain"), "plain"))
    gated = load_study(_write(tmp_path, SCREEN.replace("NAME", "gated") + "candle_gate = true\n", "gated"))

    assert "candle_gate" not in plain.plan["study"]  # an older plan hashes as before
    assert gated.plan["study"]["candle_gate"] is True  # type: ignore[index, call-overload]  # JSON object


def test_a_replay_study_takes_no_candle_gate(tmp_path):
    text = '[study]\nname = "r"\nlab = "replay"\nentry_mode = "maker"\nseed = 1\ncandle_gate = true\n'

    with pytest.raises(RunFileError, match="a replay study takes no candle_gate"):
        load_study(_write(tmp_path, CANDLE_RUN.replace("STRATEGY", "bollinger_rsi") + text, "replay"))


@pytest.mark.postgres
def test_a_candle_gate_study_has_the_lower_floor_and_holds_out(tmp_path, capsys):
    """Runs in the lab service: ``docker compose --profile lab run --rm -T --entrypoint pytest lab -q -m postgres``."""
    catalog, reports = tmp_path / "catalog", tmp_path / "reports"
    _catalog(catalog)
    settings = lab_settings(catalog, reports).model_copy(update={"git_commit": uuid4().hex + uuid4().hex[:8]})
    name = f"gate-{uuid4().hex[:8]}"
    base = Settings(trading_env="demo", postgres_user="lab_app")
    store = StudyStore(create_engine(base.database_url(read_secret(POSTGRES_PASSWORD)), pool_size=2))
    try:
        vwap = SCREEN.replace("bollinger_rsi", "vwap_retest").replace('bb_k = ["2.0"]', 'target_r = ["1.5"]')
        vwap = vwap.replace("NAME", f"vwap-{uuid4().hex[:8]}") + "candle_gate = true\n"
        assert run_study(settings, load_study(_write(tmp_path, vwap, "vwap")), store) == EXIT_USAGE
        assert "vwap_retest may not take the candle gate path" in capsys.readouterr().err

        text = SCREEN.replace("NAME", name).replace(
            'entry_mode = "maker"', f'entry_mode = "maker"\nrsi_low = {20 + uuid4().int % 9}'
        )
        assert (
            run_study(settings, load_study(_write(tmp_path, text + "candle_gate = true\n", "gate")), store) == EXIT_OK
        )
        assert run_screen(settings, name, store.engine) in (EXIT_PASS, EXIT_FAIL)
        report = json.loads((reports / f"screen-{name}" / "screen.json").read_text())
        assert report["figures"]["min_trades_per_day"] == "0.17"

        capsys.readouterr()
        run_holdout(settings, name, store, today=TODAY)  # its outcome is the backtest's; only the refusal matters
        assert "a candle screen never holds out" not in capsys.readouterr().err
    finally:
        store.engine.dispose()

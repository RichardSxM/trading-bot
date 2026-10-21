"""covers: spec 0015 AC-15 (``python -m trader.version`` prints the version the trader would record),
AC-26 (and the image's commit)."""

import json

import pytest

from trader import version
from trader.main import EXIT_CONFIG, TraderSettings
from trading_core import versioning
from trading_core.node.placeholder import STRATEGY_NAME, PlaceholderTimed


def test_the_printed_version_is_the_one_the_trader_records(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_ENV", "demo")  # Compose sets it; CI runs pytest without it
    settings = TraderSettings()
    expected = versioning.freeze(PlaceholderTimed.__module__, settings.strategy_config())

    with pytest.raises(SystemExit) as exit_info:
        version.main()

    printed = json.loads(capsys.readouterr().out)
    assert exit_info.value.code == 0
    assert printed["strategy"] == STRATEGY_NAME
    assert printed["kind"] == "scalping"
    assert (printed["code_hash"], printed["frozen_hash"]) == (expected.code_hash, expected.frozen_hash)
    assert printed["git_commit"] == settings.git_commit


def test_bad_config_exits_78_and_prints_nothing_to_stdout(monkeypatch, capsys):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("KRAKEN_KEY_CHECK_TIMEOUT_S", "-1")

    with pytest.raises(SystemExit) as exit_info:
        version.main()

    out = capsys.readouterr()
    assert exit_info.value.code == EXIT_CONFIG
    assert out.out == ""
    assert "kraken_key_check_timeout_s" in out.err

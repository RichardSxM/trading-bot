"""covers: spec 0030 AC-12 (the trader slot names: main pinned to today's, portfolio's own) and AC-14 (result
slot field, the portfolio's commands)."""

import pytest

from trading_core.control import halt, pause
from trading_core.control.slots import (
    TraderSlot,
    consumer_name,
    control_group,
    executes,
    group_start,
    halt_key,
    pause_key,
    result_slot,
    result_slot_field,
    snapshot_name,
)
from trading_core.recording.enums import ControlCmd


@pytest.mark.parametrize("env", ["demo", "live"])
def test_main_returns_exactly_today_s_names(env):
    assert pause_key(TraderSlot.MAIN, env) == pause.pause_key(env) == f"{env}:trader:pause"
    assert halt_key(TraderSlot.MAIN, env) == halt.halt_key(env) == f"{env}:trader:halt"
    for base in ("heartbeat", "trader_state", "positions", "account", "exchange"):
        assert snapshot_name(TraderSlot.MAIN, base) == base
    assert control_group(TraderSlot.MAIN) == "trader"
    assert consumer_name(TraderSlot.MAIN) == "trader"
    assert group_start(TraderSlot.MAIN) == "0"
    assert result_slot_field(TraderSlot.MAIN) == {}


def test_portfolio_has_its_own_names():
    assert pause_key(TraderSlot.PORTFOLIO, "demo") == "demo:trader:portfolio:pause"
    assert halt_key(TraderSlot.PORTFOLIO, "demo") == "demo:trader:portfolio:halt"
    assert snapshot_name(TraderSlot.PORTFOLIO, "heartbeat") == "portfolio_heartbeat"
    assert snapshot_name(TraderSlot.PORTFOLIO, "plan") == "portfolio_plan"
    assert control_group(TraderSlot.PORTFOLIO) == "trader-portfolio"
    assert consumer_name(TraderSlot.PORTFOLIO) == "trader-portfolio-1"
    assert group_start(TraderSlot.PORTFOLIO) == "$"
    assert result_slot_field(TraderSlot.PORTFOLIO) == {"slot": "portfolio"}


def test_a_result_without_a_slot_field_is_main_s():
    assert result_slot({b"id": b"x"}) is TraderSlot.MAIN
    assert result_slot({"slot": "portfolio"}) is TraderSlot.PORTFOLIO
    assert result_slot({b"slot": b"main"}) is TraderSlot.MAIN
    assert result_slot({"slot": "elsewhere"}) is None


def test_main_executes_every_command_and_portfolio_only_four():
    assert all(executes(TraderSlot.MAIN, cmd.value) for cmd in ControlCmd)
    allowed = {cmd for cmd in ControlCmd if executes(TraderSlot.PORTFOLIO, cmd.value)}
    assert allowed == {ControlCmd.STOP, ControlCmd.RESUME, ControlCmd.PAUSE, ControlCmd.UNPAUSE}

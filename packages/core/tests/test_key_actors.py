"""covers: spec 0003 AC-9 (key age warning), on Nautilus's TestClock.

Needs the ``node`` extra (NautilusTrader), so it runs in the trader image.
"""

import json
from datetime import UTC, date, datetime

import pytest

pytest.importorskip("nautilus_trader")

from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import MessageBus, TestClock
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.portfolio.portfolio import Portfolio

from trading_core.logs import configure_logging
from trading_core.node.key_actors import KeyAgeActor, KeyAgeConfig

SECOND_NS = 1_000_000_000


def _lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]


@pytest.fixture
def world():
    clock = TestClock()
    clock.set_time(int(datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp() * SECOND_NS))
    msgbus = MessageBus(TraderId("TRADER-001"), clock)
    cache = Cache()
    portfolio = Portfolio(msgbus, cache, clock)
    return clock, msgbus, cache, portfolio


def _start(actor, world):
    clock, msgbus, cache, portfolio = world
    actor.register_base(portfolio, msgbus, cache, clock)
    actor.start()
    return actor


def _advance(clock, seconds):
    for handler in clock.advance_time(clock.timestamp_ns() + seconds * SECOND_NS):
        handler.handle()


def test_key_age_warns_at_start_and_again_after_midnight(world, capsys):
    configure_logging(process="trader", env="live")
    _start(KeyAgeActor(KeyAgeConfig(rotated_on=date(2026, 7, 1))), world)  # 92 days before the clock

    _advance(world[0], 12 * 3600 + 1)  # past the next UTC midnight

    warnings = [line for line in _lines(capsys) if line.get("code") == "kraken_key_rotation_due"]
    assert [w["age_days"] for w in warnings] == [92, 93]
    assert warnings[0]["rotated_on"] == "2026-07-01"


def test_a_fresh_key_logs_no_warning(world, capsys):
    configure_logging(process="trader", env="live")
    _start(KeyAgeActor(KeyAgeConfig(rotated_on=date(2026, 9, 1))), world)

    assert not [line for line in _lines(capsys) if line.get("code") == "kraken_key_rotation_due"]


@pytest.mark.parametrize(("rotated_on", "ages"), [(date(2026, 7, 3), []), (date(2026, 7, 2), [91])])
def test_a_key_warns_only_once_it_is_older_than_90_days(world, capsys, rotated_on, ages):
    configure_logging(process="trader", env="live")

    _start(KeyAgeActor(KeyAgeConfig(rotated_on=rotated_on)), world)

    warnings = [line for line in _lines(capsys) if line.get("code") == "kraken_key_rotation_due"]
    assert [w["age_days"] for w in warnings] == ages


def test_a_stopped_actor_logs_nothing_more_after_midnight(world, capsys):
    configure_logging(process="trader", env="live")
    actor = _start(KeyAgeActor(KeyAgeConfig(rotated_on=date(2026, 7, 1))), world)
    capsys.readouterr()

    actor.stop()
    _advance(world[0], 12 * 3600 + 1)

    assert not [line for line in _lines(capsys) if line.get("code") == "kraken_key_rotation_due"]

"""covers: spec 0030 AC-12 (``TRADER_SLOT`` in the trader's settings: ``portfolio`` only in demo with
``trend_portfolio``) and AC-13 (``trader.cache reset`` checks the slot's own heartbeat and deletes only its
own keys)."""

import fnmatch
from datetime import date

import pytest
from pydantic import ValidationError

from trader.cache import heartbeat_alive, reset
from trader.main import TraderSettings, plan_snapshot_base
from trading_core.control.slots import TraderSlot, snapshot_name
from trading_core.snapshot.envelope import encode

SECOND = 1_000_000_000


def test_the_slot_defaults_to_main(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")

    assert TraderSettings().trader_slot is TraderSlot.MAIN


def test_portfolio_runs_trend_portfolio_in_demo(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("TRADER_SLOT", "portfolio")
    monkeypatch.setenv("PAPER_STRATEGY", "trend_portfolio")

    assert TraderSettings().trader_slot is TraderSlot.PORTFOLIO


def test_portfolio_refuses_any_other_strategy(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("TRADER_SLOT", "portfolio")
    monkeypatch.setenv("PAPER_STRATEGY", "six_vote_trend")

    with pytest.raises(ValidationError, match="PAPER_STRATEGY=trend_portfolio"):
        TraderSettings()


def test_portfolio_refuses_live(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("TRADER_SLOT", "portfolio")
    monkeypatch.setenv("PAPER_STRATEGY", "trend_portfolio")

    with pytest.raises(ValidationError, match="TRADER_SLOT=portfolio is allowed only"):
        TraderSettings(kraken_key_rotated_on=date(2026, 1, 1))


def test_an_unknown_slot_is_refused(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("TRADER_SLOT", "third")

    with pytest.raises(ValidationError):
        TraderSettings()


class FakeRedis:
    def __init__(self, *keys):
        self.keys, self.values = set(keys), {}

    def get(self, name):
        return self.values.get(name)

    def scan_iter(self, match, count):
        return iter([k for k in sorted(self.keys) if fnmatch.fnmatchcase(k, match)])

    def unlink(self, *names):
        gone = self.keys & set(names)
        self.keys -= gone
        return len(gone)


def test_the_portfolio_reset_reads_its_own_heartbeat():
    redis = FakeRedis()
    now = 100 * SECOND
    redis.values["demo:snapshot:heartbeat"] = encode(now - SECOND, {})  # main runs

    assert not heartbeat_alive(redis, "demo", lambda: now, slot=TraderSlot.PORTFOLIO)

    redis.values["demo:snapshot:portfolio_heartbeat"] = encode(now - SECOND, {})
    assert heartbeat_alive(redis, "demo", lambda: now, slot=TraderSlot.PORTFOLIO)


def test_the_portfolio_reset_deletes_only_its_own_cache_keys():
    redis = FakeRedis(
        "trader-PORTFOLIO-DEMO:orders:O-1",
        "trader-TRADER-DEMO:orders:O-2",
        "demo:reconcile:cache_untrusted",
        "demo:trader:portfolio:halt",
    )

    deleted = reset(redis, "PORTFOLIO-DEMO", "demo", alive=lambda: False, slot=TraderSlot.PORTFOLIO)

    assert deleted == 1
    assert redis.keys == {
        "trader-TRADER-DEMO:orders:O-2",
        "demo:reconcile:cache_untrusted",
        "demo:trader:portfolio:halt",
    }


@pytest.mark.parametrize("slot", list(TraderSlot))
def test_the_plan_snapshot_is_portfolio_plan_in_either_slot(slot):
    """covers: spec 0030 AC-13 (the dashboard and the alerter read ``portfolio_plan``; a laptop run on 2026-11-07
    found the portfolio slot writing ``portfolio_portfolio_plan``)"""
    assert snapshot_name(slot, plan_snapshot_base(slot)) == "portfolio_plan"

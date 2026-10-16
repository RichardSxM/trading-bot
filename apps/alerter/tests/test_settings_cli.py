"""covers: spec 0013 AC-20 (Pushover and ping secrets only in pushover mode), the ALERTS_* validation, AC-17
(``alerter test``: one marked push per tier, a JSON line each, exit 1 on a failed push), and that the
alerter never imports NautilusTrader."""

import asyncio
import json
import sys

import pytest
from pydantic import ValidationError

from alerter.cli import ALL_TIERS, send_tests
from alerter.settings import AlerterSettings, alerter_secrets
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Tier

from .conftest import FakeDeliverer, FakeStore


def test_dry_run_needs_only_the_two_passwords(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")

    settings = AlerterSettings()

    assert settings.alerts_delivery == "log"
    assert alerter_secrets(settings) == ("postgres_password", "redis_password")


def test_pushover_mode_needs_the_pushover_files_and_the_ping_url(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("ALERTS_DELIVERY", "pushover")

    assert set(alerter_secrets(AlerterSettings())) == {
        "postgres_password",
        "redis_password",
        "pushover_app_token",
        "pushover_user_key",
        "hc_alerter_url",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALERTS_DELIVERY", "email"),
        ("ALERTS_DAILY_AT", "8am"),
        ("ALERTS_DAILY_TZ", "Mars/Olympus"),
        ("ALERTS_TRADER_DOWN_S", "0"),
    ],
)
def test_bad_alert_settings_refuse(monkeypatch, name, value):
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        AlerterSettings()


def _tests(delivery, tiers=ALL_TIERS, store=None):
    deliverer = FakeDeliverer(delivery)
    status = asyncio.run(send_tests(AlertEngine(EngineConfig(env="demo")), deliverer, store, tiers))
    return status, deliverer


def test_alerter_test_sends_one_marked_push_per_tier_loudest_first(capsys):
    store = FakeStore()

    status, deliverer = _tests(Delivery.DRY_RUN, store=store)

    assert status == 0
    assert [p.tier for p in deliverer.sent] == [Tier.CRITICAL, Tier.WARNING, Tier.INFO]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["tier"], line["delivery"], line["stored"]) for line in lines] == [
        ("critical", "dry_run", True),
        ("warning", "dry_run", True),
        ("info", "dry_run", True),
    ]
    assert all(e.rule == "test" and e.delivery is Delivery.DRY_RUN and e.resolved_at for e in store.saves)


def test_alerter_test_with_one_tier_and_a_failed_push_exits_1(capsys):
    status, deliverer = _tests(Delivery.FAILED, tiers=[Tier.WARNING])

    assert status == 1
    assert [p.tier for p in deliverer.sent] == [Tier.WARNING]


def test_a_down_postgres_still_sends_the_test(capsys):
    store = FakeStore()
    store.down = True

    status, _ = _tests(Delivery.DRY_RUN, tiers=[Tier.INFO], store=store)

    assert status == 0
    assert json.loads(capsys.readouterr().out)["stored"] is False


def test_the_alerter_does_not_import_nautilus():
    import alerter.cli
    import alerter.main  # noqa: F401

    assert "nautilus_trader" not in sys.modules

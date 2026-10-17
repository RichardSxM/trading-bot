"""covers: spec 0013 AC-20 (Pushover and ping secrets only in pushover mode), the ALERTS_* validation, AC-17
(``alerter test``: one marked push per tier, a JSON line each, exit 1 on a failed push), and that the
alerter never imports NautilusTrader."""

import asyncio
import json
import sys

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from alerter import main
from alerter.cli import send_tests
from alerter.delivery import LogDelivery, PushoverDelivery
from alerter.service import ALL_TIERS
from alerter.settings import AlerterSettings, alerter_secrets, engine_config
from alerter.watchdog import Watchdog
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


def test_the_engine_takes_the_spec_defaults(monkeypatch):
    # covers: AC-1, AC-9, AC-10, AC-6 defaults (45 s, 1 h, 4 h, 30 per hour, 1 h block)
    monkeypatch.setenv("TRADING_ENV", "demo")

    settings = AlerterSettings()
    config = engine_config(settings)

    assert settings.alerts_trader_down_s == 45
    assert settings.alerts_disk_free_gb == 5
    assert (config.env, config.renotify_s, config.critical_renotify_s, config.hourly_cap, config.entry_block_s) == (
        "demo",
        3600,
        14_400,
        30,
        3600,
    )


def test_the_engine_takes_each_alerts_setting(monkeypatch):
    monkeypatch.setenv("TRADING_ENV", "live")
    monkeypatch.setenv("ALERTS_RENOTIFY_S", "120")
    monkeypatch.setenv("ALERTS_CRITICAL_RENOTIFY_S", "7200")
    monkeypatch.setenv("ALERTS_HOURLY_CAP", "5")
    monkeypatch.setenv("ALERTS_ENTRY_BLOCK_S", "60")

    config = engine_config(AlerterSettings())

    assert (config.env, config.renotify_s, config.critical_renotify_s, config.hourly_cap, config.entry_block_s) == (
        "live",
        120,
        7200,
        5,
        60,
    )


SECRETS = {
    "pushover_app_token": SecretStr("azGDORePK8gMaC0QOYAMyEEuzJnyUi"),
    "pushover_user_key": SecretStr("uQiRzpo4DXghDmr9QzzfQu27cmVRsG"),
    "hc_alerter_url": SecretStr("https://hc-ping.com/0f3c3a0e"),
}


def test_dry_run_builds_the_log_deliverer_and_no_watchdog(monkeypatch):
    # covers: AC-13, nothing reaches Pushover and no healthchecks.io ping is sent
    monkeypatch.setenv("TRADING_ENV", "demo")
    settings = AlerterSettings()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    assert isinstance(main.deliverer(settings, {}, http), LogDelivery)
    assert main.watchdog(settings, {}, http) is None


def test_pushover_mode_builds_pushover_and_the_watchdog(monkeypatch):
    # covers: AC-2, AC-17
    monkeypatch.setenv("TRADING_ENV", "demo")
    monkeypatch.setenv("ALERTS_DELIVERY", "pushover")
    settings = AlerterSettings()
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    assert isinstance(main.deliverer(settings, SECRETS, http), PushoverDelivery)
    assert isinstance(main.watchdog(settings, SECRETS, http), Watchdog)

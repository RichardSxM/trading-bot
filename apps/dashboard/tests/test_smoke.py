"""covers: the dashboard process basics: health, no NautilusTrader, docs off, boot wiring (spec 0004 AC-5)."""

import sys

import uvicorn


def test_healthz_answers_200_without_a_session(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_does_not_import_nautilus():
    assert "nautilus_trader" not in sys.modules


def test_api_docs_and_schema_are_not_served(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_unknown_route_is_404(client):
    assert client.get("/nope").status_code == 404


def test_main_boots_with_the_login_secrets_and_guard_then_serves_create_app(monkeypatch):
    import dashboard.main as dashboard_main
    from dashboard.settings import DashboardSettings, credentials_guard
    from trading_core.boot import Boot

    from .conftest import SECRETS

    calls = []
    started = Boot(DashboardSettings(trading_env="demo"), SECRETS)

    def fake_boot(kind, **kw):
        calls.append(("boot", kind, kw))
        return started

    monkeypatch.setattr(dashboard_main, "boot", fake_boot)
    monkeypatch.setattr(dashboard_main, "create_app", lambda settings, secrets: ("app", settings, secrets))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(("run", app, kw)))
    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("DASHBOARD_PORT", "9001")

    dashboard_main.main()

    assert calls[0] == (
        "boot",
        "dashboard",
        {
            "settings_cls": DashboardSettings,
            "secrets": (
                "postgres_password",
                "redis_password",
                "dashboard_password_hash",
                "dashboard_totp_seed",
                "dashboard_session_key",
            ),
            "guard": credentials_guard,
        },
    )
    assert calls[1] == (
        "run",
        ("app", started.settings, SECRETS),
        {"host": "127.0.0.1", "port": 9001, "log_config": None, "proxy_headers": False},
    )


def test_main_listens_on_all_container_interfaces_on_8000_by_default(monkeypatch):
    import dashboard.main as dashboard_main
    from dashboard.settings import DashboardSettings
    from trading_core.boot import Boot

    seen = {}
    monkeypatch.setattr(dashboard_main, "boot", lambda kind, **kw: Boot(DashboardSettings(trading_env="demo"), {}))
    monkeypatch.setattr(dashboard_main, "create_app", lambda settings, secrets: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)

    dashboard_main.main()

    assert (seen["host"], seen["port"]) == ("0.0.0.0", 8000)

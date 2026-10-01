import sys

import uvicorn
from fastapi.testclient import TestClient

from dashboard.main import app


def test_healthz_answers_200():
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_does_not_import_nautilus():
    assert "nautilus_trader" not in sys.modules


def test_api_docs_and_schema_are_not_served():
    client = TestClient(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_unknown_route_is_404():
    assert TestClient(app).get("/nope").status_code == 404


def test_main_boots_as_dashboard_then_serves_without_uvicorn_log_config(monkeypatch):
    import dashboard.main as dashboard_main

    calls = []
    monkeypatch.setattr(dashboard_main, "boot", lambda kind: calls.append(("boot", kind)))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(("run", kw)))
    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
    monkeypatch.setenv("DASHBOARD_PORT", "9001")

    dashboard_main.main()

    assert calls == [("boot", "dashboard"), ("run", {"host": "127.0.0.1", "port": 9001, "log_config": None})]


def test_main_listens_on_all_container_interfaces_on_8000_by_default(monkeypatch):
    import dashboard.main as dashboard_main

    seen = {}
    monkeypatch.setattr(dashboard_main, "boot", lambda kind: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.delenv("DASHBOARD_HOST", raising=False)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)

    dashboard_main.main()

    assert (seen["host"], seen["port"]) == ("0.0.0.0", 8000)

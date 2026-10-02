"""Dashboard entry point: boot with the login secrets and their guard, then serve ``create_app``."""

import os

import uvicorn

from dashboard.settings import DASHBOARD_SECRETS, DashboardSettings, credentials_guard
from dashboard.web.app import create_app
from trading_core.boot import boot


def main() -> None:
    # The dashboard always starts: the page with the stop button must come up even
    # when time servers do not answer, so its clock check only warns.
    started = boot("dashboard", settings_cls=DashboardSettings, secrets=DASHBOARD_SECRETS, guard=credentials_guard)
    # Inside the container uvicorn listens on all container interfaces; Compose
    # publishes the port on the host's 127.0.0.1 only, and tailscale serve exposes it.
    # proxy_headers stays off: the lockout reads the real socket peer (spec 0004).
    uvicorn.run(
        create_app(started.settings, started.secrets),
        host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        port=int(os.environ.get("DASHBOARD_PORT", "8000")),
        log_config=None,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

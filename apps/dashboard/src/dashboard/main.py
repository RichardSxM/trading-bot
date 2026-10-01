"""Dashboard entry point. The scaffold serves only /healthz; pages arrive with feature 5."""

import os

import uvicorn
from fastapi import FastAPI

from trading_core.boot import boot
from trading_core.secrets import POSTGRES_PASSWORD, REDIS_PASSWORD

app = FastAPI(title="Kraken Futures Scalper", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def main() -> None:
    # The dashboard always starts: the page with the stop button must come up even
    # when time servers do not answer, so its clock check only warns.
    boot("dashboard", secrets=(POSTGRES_PASSWORD, REDIS_PASSWORD))
    # Inside the container uvicorn listens on all container interfaces; Compose
    # publishes the port on the host's 127.0.0.1 only, and tailscale serve exposes it.
    uvicorn.run(
        app,
        host=os.environ.get("DASHBOARD_HOST", "0.0.0.0"),
        port=int(os.environ.get("DASHBOARD_PORT", "8000")),
        log_config=None,
    )


if __name__ == "__main__":
    main()

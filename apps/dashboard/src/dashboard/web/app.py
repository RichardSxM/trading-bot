"""``create_app``: the FastAPI app with its templates, auth, Redis client, and poller (spec 0004)."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr
from redis.asyncio import Redis

from dashboard.auth.lockout import Lockout
from dashboard.auth.password import PasswordChecker
from dashboard.auth.proxy import default_gateway
from dashboard.auth.session import SessionCodec
from dashboard.auth.totp import TotpGuard
from dashboard.live.poller import ACTIVITY_SIZE, Poller, supervise
from dashboard.live.state import ActivityRow, AgeView, CommandView
from dashboard.settings import DashboardSettings, normalize_seed
from dashboard.ui.format import num, pnl, ts
from dashboard.web import auth_routes, control_routes, page_routes
from dashboard.web.context import AppContext, LiveRedis
from dashboard.web.middleware import ShellMiddleware
from dashboard.web.session import CsrfRefused, LoginRequired, handle_login_required
from trading_core.control.messages import control_stream, results_stream
from trading_core.secrets import (
    DASHBOARD_PASSWORD_HASH,
    DASHBOARD_SESSION_KEY,
    DASHBOARD_TOTP_SEED,
    REDIS_PASSWORD,
    fingerprint,
)
from trading_core.snapshot.envelope import snapshot_key

PACKAGE_DIR: Final = Path(__file__).resolve().parent.parent
# The stop path must answer fast even when Redis hangs (spec 0004 AC-13).
REDIS_TIMEOUT_S: Final = 1.0
# Only the newest results are searched for the latest command (AC-12).
RESULTS_SEARCHED: Final = 200
ROUTE_TABLE: Final = Path("/proc/net/route")

log = logging.getLogger(__name__)


def trusted_peers(route_table: Path = ROUTE_TABLE) -> frozenset[str]:
    """The container's default gateway, where Docker's published port delivers host connections.

    Empty when the route table is missing (not Linux) or has no default route.
    """
    try:
        gateway = default_gateway(route_table.read_text())
    except OSError:
        gateway = None
    log.info("trusted proxy", extra={"fields": {"code": "trusted_proxy", "gateway": gateway}})
    return frozenset() if gateway is None else frozenset({gateway})


def redis_client(settings: DashboardSettings, password: SecretStr) -> Redis:
    """The redis-py client as the ``dashboard`` ACL user, with 1 s connect and socket timeouts."""
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        username=settings.redis_username,
        password=password.get_secret_value(),
        socket_timeout=REDIS_TIMEOUT_S,
        socket_connect_timeout=REDIS_TIMEOUT_S,
    )


def create_app(
    settings: DashboardSettings,
    secrets: Mapping[str, SecretStr],
    *,
    redis: LiveRedis | None = None,
    clock: Callable[[], float] = time.time,
    clock_ns: Callable[[], int] = time.time_ns,
) -> FastAPI:
    """Build the dashboard app. Tests pass fake settings, secrets, a fake Redis, and fake clocks."""
    client: LiveRedis = redis if redis is not None else redis_client(settings, secrets[REDIS_PASSWORD])
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(num=num, pnl=pnl, ts=ts)
    age_template = templates.get_template("partials/topbar_age.html")
    command_template = templates.get_template("partials/topbar_command.html")
    activity_template = templates.get_template("partials/overview_activity.html")
    heartbeat_key = snapshot_key(settings.trading_env, "heartbeat")
    password_hash = secrets[DASHBOARD_PASSWORD_HASH].get_secret_value()

    commands = control_stream(settings.trading_env)
    results = results_stream(settings.trading_env)

    def render_age(view: AgeView) -> str:
        return age_template.render(view=view)

    def render_command(view: CommandView) -> str:
        return command_template.render(view=view)

    def render_activity(rows: Sequence[ActivityRow] | None) -> str:
        return activity_template.render(rows=rows)

    context = AppContext(
        settings=settings,
        templates=templates,
        codec=SessionCodec(
            secrets[DASHBOARD_SESSION_KEY].get_secret_value(),
            fingerprint(password_hash),
            idle_s=settings.dashboard_session_idle_h * 3600,
            max_s=settings.dashboard_session_max_d * 86400,
        ),
        passwords=PasswordChecker(password_hash),
        totp=TotpGuard(normalize_seed(secrets[DASHBOARD_TOTP_SEED].get_secret_value())),
        lockout=Lockout(),
        poller=Poller(
            lambda: client.get(heartbeat_key),
            render_age,
            interval_s=settings.dashboard_poll_ms / 1000,
            amber_s=settings.dashboard_stale_amber_s,
            red_s=settings.dashboard_stale_red_s,
            read_command=lambda: client.xrevrange(commands, count=ACTIVITY_SIZE),
            read_results=lambda: client.xrevrange(results, count=RESULTS_SEARCHED),
            render_command=render_command,
            render_activity=render_activity,
            hide_after_s=settings.dashboard_command_hide_s,
            clock_ns=clock_ns,
        ),
        redis=client,
        clock=clock,
        trusted_peers=trusted_peers(),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        poller_task = asyncio.create_task(supervise(context.poller))
        try:
            yield
        finally:
            poller_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poller_task
            await client.aclose()

    app = FastAPI(
        title="Kraken Futures Scalper",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.add_middleware(ShellMiddleware, codec=context.codec, clock=clock, secure=settings.dashboard_cookie_secure)
    app.add_exception_handler(LoginRequired, handle_login_required)
    app.add_exception_handler(CsrfRefused, control_routes.handle_csrf_refused_on_stop)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_routes.router)
    app.include_router(page_routes.router)
    app.include_router(control_routes.router)
    return app

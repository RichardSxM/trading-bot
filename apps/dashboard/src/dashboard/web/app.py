"""``create_app``: the app with its templates, auth, Redis, Postgres reads, and poller (specs 0004, 0006)."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dashboard.auth.lockout import Lockout
from dashboard.auth.password import PasswordChecker
from dashboard.auth.proxy import default_gateway
from dashboard.auth.session import SessionCodec
from dashboard.auth.totp import TotpGuard
from dashboard.data.pnl import PnlFigures, pnl_figures
from dashboard.data.trades import TradesView, trades_view
from dashboard.live.poller import ACTIVITY_SIZE, Poller, supervise
from dashboard.live.position import PositionView
from dashboard.live.state import (
    ActivityRow,
    AgeView,
    AlertView,
    BlockedView,
    CommandView,
    LossesView,
    PauseView,
    collapse,
)
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
    POSTGRES_PASSWORD,
    REDIS_PASSWORD,
    fingerprint,
)
from trading_core.snapshot.envelope import snapshot_key

PACKAGE_DIR: Final = Path(__file__).resolve().parent.parent
# The stop path must answer fast even when Redis hangs (spec 0004 AC-13).
REDIS_TIMEOUT_S: Final = 1.0
# Only the newest results are searched for the latest command (AC-12).
RESULTS_SEARCHED: Final = 200
# Where the red stop alerts send you when the bot will not stop: close positions on Kraken directly (AC-21).
KRAKEN_FUTURES_URL: Final = {"demo": "https://demo-futures.kraken.com/", "live": "https://futures.kraken.com/"}
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
    read_pnl: Callable[[], PnlFigures] | None = None,
    read_trades: Callable[[int, int], TradesView] | None = None,
) -> FastAPI:
    """Build the dashboard app. Tests pass fake settings, secrets, a fake Redis, fake clocks, and Postgres readers.

    ``read_pnl`` and ``read_trades`` are blocking (they query Postgres as ``dashboard_app``); the
    poller and the Trades route run them in a thread.
    """
    client: LiveRedis = redis if redis is not None else redis_client(settings, secrets[REDIS_PASSWORD])
    engine = None
    env = settings.trading_env
    if read_pnl is None or read_trades is None:
        # Two connections: a Trades page load never waits behind the poller's P&L read.
        engine = create_engine(
            settings.database_url(secrets[POSTGRES_PASSWORD]), pool_size=2, max_overflow=0, pool_pre_ping=True
        )
        sessions = sessionmaker(engine)

        def postgres_pnl() -> PnlFigures:
            return pnl_figures(sessions, env, datetime.fromtimestamp(clock(), UTC))

        def postgres_trades(page: int, fills_page: int) -> TradesView:
            return trades_view(sessions, env, page, fills_page)

        read_pnl = read_pnl or postgres_pnl
        read_trades = read_trades or postgres_trades
    pnl_reader, trades_reader = read_pnl, read_trades
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(num=num, pnl=pnl, ts=ts)
    age_template = templates.get_template("partials/topbar_age.html")
    command_template = templates.get_template("partials/topbar_command.html")
    alert_template = templates.get_template("partials/topbar_alert.html")
    activity_template = templates.get_template("partials/overview_activity.html")
    pause_template = templates.get_template("partials/topbar_pause.html")
    position_template = templates.get_template("partials/overview_position.html")
    blocked_template = templates.get_template("partials/topbar_blocked.html")
    blocked_reasons_template = templates.get_template("partials/overview_blocked.html")
    losses_template = templates.get_template("partials/system_losses.html")
    position_key = snapshot_key(settings.trading_env, "position")
    account_key = snapshot_key(settings.trading_env, "account")
    heartbeat_key = snapshot_key(settings.trading_env, "heartbeat")
    trader_state_key = snapshot_key(settings.trading_env, "trader_state")
    password_hash = secrets[DASHBOARD_PASSWORD_HASH].get_secret_value()

    commands = control_stream(settings.trading_env)
    results = results_stream(settings.trading_env)

    def render_age(view: AgeView) -> str:
        return age_template.render(view=view)

    def render_command(view: CommandView) -> str:
        return command_template.render(view=view)

    kraken_url = KRAKEN_FUTURES_URL[settings.trading_env]

    def render_alert(view: AlertView | None) -> str:
        return alert_template.render(view=view, kraken_url=kraken_url)

    def render_activity(rows: Sequence[ActivityRow] | None) -> str:
        return activity_template.render(groups=None if rows is None else collapse(rows))

    def render_pause(view: PauseView) -> str:
        return pause_template.render(view=view)

    def render_position(view: PositionView) -> str:
        return position_template.render(view=view)

    def render_blocked(view: BlockedView) -> str:
        return blocked_template.render(view=view)

    def render_blocked_reasons(view: BlockedView) -> str:
        return blocked_reasons_template.render(view=view)

    def render_losses(view: LossesView) -> str:
        return losses_template.render(view=view)

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
            render_alert=render_alert,
            render_activity=render_activity,
            read_trader_state=lambda: client.get(trader_state_key),
            render_pause=render_pause,
            hide_after_s=settings.dashboard_command_hide_s,
            ack_warn_s=settings.dashboard_ack_warn_s,
            read_timeout_s=REDIS_TIMEOUT_S,
            clock_ns=clock_ns,
            read_position=lambda: client.get(position_key),
            read_account=lambda: client.get(account_key),
            read_pnl=lambda: asyncio.to_thread(pnl_reader),
            render_position=render_position,
            render_blocked=render_blocked,
            render_blocked_reasons=render_blocked_reasons,
            render_losses=render_losses,
        ),
        redis=client,
        clock=clock,
        read_trades=lambda page, fills_page: asyncio.to_thread(trades_reader, page, fills_page),
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
            if engine is not None:
                engine.dispose()

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

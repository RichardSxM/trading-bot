"""``create_app``: the app with its templates, auth, Redis, Postgres reads, and poller (specs 0004, 0006)."""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

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
from dashboard.data.alerts import AlertLine, recent_alerts
from dashboard.data.pnl import PnlFigures, pnl_figures
from dashboard.data.reconciliations import reconciliations_view
from dashboard.data.strategies import StrategiesView, promotions_view, strategies_view, study_view
from dashboard.data.trades import TradesView, trades_view
from dashboard.live.alerts import MuteView
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
    ReconcileView,
    collapse,
)
from dashboard.settings import DashboardSettings, normalize_seed
from dashboard.ui.format import local_hm, num, pnl, ts
from dashboard.web import alert_routes, auth_routes, control_routes, page_routes
from dashboard.web.context import AppContext, LiveRedis
from dashboard.web.middleware import ShellMiddleware
from dashboard.web.session import CsrfRefused, LoginRequired, handle_login_required
from trading_core.control.alerts import alerts_results_stream
from trading_core.control.messages import control_stream, results_stream
from trading_core.recording.gate_queries import PromotionLine, StudyDetail
from trading_core.recording.queries import ReconciliationLine
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
    read_strategies: Callable[[], StrategiesView] | None = None,
    read_study: Callable[[UUID], StudyDetail | None] | None = None,
    read_promotions: Callable[[int], Sequence[PromotionLine]] | None = None,
    read_alerts: Callable[[], Sequence[AlertLine]] | None = None,
    read_reconciliations: Callable[[int], Sequence[ReconciliationLine]] | None = None,
) -> FastAPI:
    """Build the dashboard app. Tests pass fake settings, secrets, a fake Redis, fake clocks, and Postgres readers.

    ``read_pnl``, ``read_trades``, ``read_strategies``, ``read_alerts``, and ``read_reconciliations`` are
    blocking (they query Postgres as ``dashboard_app``); the poller and the page routes run them in a thread.
    """
    client: LiveRedis = redis if redis is not None else redis_client(settings, secrets[REDIS_PASSWORD])
    engine = None
    env = settings.trading_env
    if (
        read_pnl is None
        or read_trades is None
        or read_alerts is None
        or read_strategies is None
        or read_study is None
        or read_promotions is None
        or read_reconciliations is None
    ):
        # Two connections: a Trades page load never waits behind the poller's P&L read.
        engine = create_engine(
            settings.database_url(secrets[POSTGRES_PASSWORD]), pool_size=2, max_overflow=0, pool_pre_ping=True
        )
        sessions = sessionmaker(engine)

        def postgres_pnl() -> PnlFigures:
            return pnl_figures(sessions, env, datetime.fromtimestamp(clock(), UTC))

        def postgres_trades(page: int, fills_page: int) -> TradesView:
            return trades_view(sessions, env, page, fills_page)

        def postgres_strategies() -> StrategiesView:
            return strategies_view(sessions, datetime.fromtimestamp(clock(), UTC))

        def postgres_study(study_id: UUID) -> StudyDetail | None:
            return study_view(sessions, study_id)

        def postgres_promotions(page: int) -> Sequence[PromotionLine]:
            return promotions_view(sessions, page)

        def postgres_reconciliations(page: int) -> Sequence[ReconciliationLine]:
            return reconciliations_view(sessions, env, page)

        def postgres_alerts() -> Sequence[AlertLine]:
            return recent_alerts(sessions, env, datetime.fromtimestamp(clock(), UTC))

        read_pnl = read_pnl or postgres_pnl
        read_trades = read_trades or postgres_trades
        read_strategies = read_strategies or postgres_strategies
        read_study = read_study or postgres_study
        read_promotions = read_promotions or postgres_promotions
        read_alerts = read_alerts or postgres_alerts
        read_reconciliations = read_reconciliations or postgres_reconciliations
    pnl_reader, trades_reader, alerts_reader = read_pnl, read_trades, read_alerts
    strategies_reader, study_reader, promotions_reader = read_strategies, read_study, read_promotions
    reconciliations_reader = read_reconciliations
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.filters.update(num=num, pnl=pnl, ts=ts, local_hm=local_hm)
    age_template = templates.get_template("partials/topbar_age.html")
    command_template = templates.get_template("partials/topbar_command.html")
    alert_template = templates.get_template("partials/topbar_alert.html")
    activity_template = templates.get_template("partials/overview_activity.html")
    pause_template = templates.get_template("partials/topbar_pause.html")
    position_template = templates.get_template("partials/overview_position.html")
    blocked_template = templates.get_template("partials/topbar_blocked.html")
    blocked_reasons_template = templates.get_template("partials/overview_blocked.html")
    mute_template = templates.get_template("partials/overview_mute.html")
    losses_template = templates.get_template("partials/system_losses.html")
    check_template = templates.get_template("partials/topbar_check.html")
    position_key = snapshot_key(settings.trading_env, "position")
    account_key = snapshot_key(settings.trading_env, "account")
    heartbeat_key = snapshot_key(settings.trading_env, "heartbeat")
    trader_state_key = snapshot_key(settings.trading_env, "trader_state")
    exchange_key = snapshot_key(settings.trading_env, "exchange")
    password_hash = secrets[DASHBOARD_PASSWORD_HASH].get_secret_value()

    commands = control_stream(settings.trading_env)
    results = results_stream(settings.trading_env)
    alert_results = alerts_results_stream(settings.trading_env)

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

    def render_mute(view: MuteView) -> str:
        return mute_template.render(view=view)

    def render_check(view: ReconcileView) -> str:
        return check_template.render(view=view)

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
            read_alerts=lambda: asyncio.to_thread(alerts_reader),
            read_alert_results=lambda: client.xrevrange(alert_results, count=RESULTS_SEARCHED),
            render_mute=render_mute,
            render_check=render_check,
            read_exchange=lambda: client.get(exchange_key),
            check_interval_s=settings.reconcile_interval_s,
        ),
        redis=client,
        clock=clock,
        read_trades=lambda page, fills_page: asyncio.to_thread(trades_reader, page, fills_page),
        read_strategies=lambda: asyncio.to_thread(strategies_reader),
        read_study=lambda study_id: asyncio.to_thread(study_reader, study_id),
        read_promotions=lambda page: asyncio.to_thread(promotions_reader, page),
        read_reconciliations=lambda page: asyncio.to_thread(reconciliations_reader, page),
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
    app.add_exception_handler(CsrfRefused, alert_routes.handle_csrf_refused)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_routes.router)
    app.include_router(page_routes.router)
    app.include_router(control_routes.router)
    app.include_router(alert_routes.router)
    return app

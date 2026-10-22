"""The shell pages, the styleguide, and the live value stream (spec 0004 AC-7, AC-9, AC-12, AC-16).

Overview, Trades, Strategies, and System sit in the nav; the styleguide is reachable by URL only.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Annotated, Final
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from markupsafe import Markup
from sqlalchemy.exc import SQLAlchemyError

from dashboard.auth.next_path import referer_page
from dashboard.auth.session import Session
from dashboard.data.strategies import StrategiesView
from dashboard.live.poller import Event, Poller
from dashboard.web.context import AppContext, app_context
from dashboard.web.session import SessionDep, session_end_cue
from dashboard.web.strategies import detail_context, log_context, strategies_context
from dashboard.web.styleguide import styleguide_context
from dashboard.web.trades import trades_context

router = APIRouter()
log = logging.getLogger(__name__)

PING_EVERY_S: Final = 2.0
# Path → (template, title) for every page inside the shell.
PAGES: Final[dict[str, tuple[str, str]]] = {
    "/": ("pages/overview.html", "Overview"),
    "/trades": ("pages/trades.html", "Trades"),
    "/strategies": ("pages/strategies.html", "Strategies"),
    "/system": ("pages/system.html", "System"),
    "/styleguide": ("pages/styleguide.html", "Styleguide"),
}
# (path, label, icon) for the side nav and the phone tab bar, in order (AC-6).
NAV: Final = (
    ("/", "Overview", "activity"),
    ("/trades", "Trades", "arrow-left-right"),
    ("/strategies", "Strategies", "flask-conical"),
    ("/system", "System", "server-cog"),
)
TRADES: Final = "pages/trades.html"
# Template → the extra context it renders (sample data for the styleguide; Trades reads Postgres).
PAGE_DATA: Final[dict[str, Callable[[], Mapping[str, object]]]] = {
    "pages/styleguide.html": styleguide_context,
}


async def trades_data(context: AppContext, page: int = 1, fills_page: int = 1) -> Mapping[str, object]:
    """The Trades lists for these pages, or ``trades_error`` when Postgres cannot be read (logged)."""
    try:
        view = await context.read_trades(page, fills_page)
    except SQLAlchemyError as exc:
        log.warning("trades read failed", extra={"fields": {"code": "trades_read_failed", "error": type(exc).__name__}})
        return {"trades_error": True}
    return trades_context(view, page, fills_page)


async def render_page(
    request: Request,
    context: AppContext,
    session: Session,
    template: str,
    title: str,
    *,
    path: str | None = None,
    errors: Mapping[str, str] | None = None,
    status_code: int = 200,
    live: Mapping[str, str] | None = None,
    data: Mapping[str, object] | None = None,
    confirm: str | None = None,
) -> Response:
    """A page inside the shell, its live partials read from Redis right now.

    ``path`` marks the current nav item (the request path when None). ``errors`` renders a
    control area in its ``NOT sent`` state after a plain form post failed: ``stop_error`` for the
    stop, ``pause_error`` for Pause, Resume, and Flatten, ``alerts_error`` for the alert controls
    (whose ``alerts_note`` says what a plain form post did). ``confirm`` (``resume`` or ``flatten``)
    renders the Trading area in that command's confirm step. ``live`` replaces the Redis read with these
    partials (a failed send already proved Redis down). ``data`` is the page's own context; when
    None, Trades reads its first pages and other pages take ``PAGE_DATA``.
    """
    partials = live if live is not None else await context.poller.render_now()
    if data is None:
        if template == TRADES:
            data = await trades_data(context)
        else:
            data = PAGE_DATA[template]() if template in PAGE_DATA else {}
    return context.templates.TemplateResponse(
        request,
        template,
        {
            "env": context.settings.trading_env,
            "title": title,
            "nav": NAV,
            "path": request.url.path if path is None else path,
            "stale_s": (context.settings.dashboard_stale_amber_s, context.settings.dashboard_stale_red_s),
            "csrf": session.csrf,
            "session_end": session_end_cue(context, session),
            "stop_error": (errors or {}).get("stop_error"),
            "pause_error": (errors or {}).get("pause_error"),
            "alerts_error": (errors or {}).get("alerts_error"),
            "alerts_note": (errors or {}).get("alerts_note"),
            "confirm": confirm,
            # Rendered by our own autoescaping templates.
            "live": {name: Markup(html) for name, html in partials.items()},
            **data,
        },
        status_code=status_code,
    )


async def render_referring_page(
    request: Request,
    context: AppContext,
    session: Session,
    *,
    errors: Mapping[str, str] | None = None,
    status_code: int,
    live: Mapping[str, str] | None = None,
    confirm: str | None = None,
) -> Response:
    """The page the ``Referer`` names (Overview when unknown), with a control area in its error or confirm state."""
    path = referer_page(request.headers.get("referer"))
    template, title = PAGES[path]
    return await render_page(
        request,
        context,
        session,
        template,
        title,
        path=path,
        errors=errors,
        status_code=status_code,
        live=live,
        confirm=confirm,
    )


@router.get("/")
async def overview(request: Request, session: SessionDep) -> Response:
    context = app_context(request)
    return await render_page(request, context, session, *PAGES["/"])


@router.get("/trades")
async def trades(
    request: Request,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    fills_page: Annotated[int, Query(ge=1)] = 1,
) -> Response:
    """Closed round trips (``page``) and latest fills (``fills_page``), 50 each; 422 below 1 or not an integer."""
    context = app_context(request)
    data = await trades_data(context, page, fills_page)
    return await render_page(request, context, session, *PAGES["/trades"], data=data)


def _read_failed(what: str, exc: SQLAlchemyError) -> None:
    log.warning(f"{what} read failed", extra={"fields": {"code": f"{what}_read_failed", "error": type(exc).__name__}})


async def strategies_data(context: AppContext) -> Mapping[str, object]:
    """The ranking and paper progress, or ``strategies_error`` when Postgres cannot be read (logged)."""
    try:
        view = await context.read_strategies()
    except SQLAlchemyError as exc:
        _read_failed("strategies", exc)
        return {"strategies_error": True, **strategies_context(StrategiesView(()))}
    return strategies_context(view)


@router.get("/strategies")
async def strategies(request: Request, session: SessionDep) -> Response:
    """Every study ranked by walk forward Sharpe (spec 0015 AC-18) and paper progress (AC-21), read when requested."""
    context = app_context(request)
    data = await strategies_data(context)
    return await render_page(request, context, session, *PAGES["/strategies"], data=data)


@router.get("/strategies/log")
async def promotion_log_page(request: Request, session: SessionDep, page: Annotated[int, Query(ge=1)] = 1) -> Response:
    """Every promotion, newest first, 50 per page (spec 0015 AC-20); 422 below 1 or not an integer."""
    context = app_context(request)
    try:
        data = log_context(await context.read_promotions(page), page)
    except SQLAlchemyError as exc:
        _read_failed("promotions", exc)
        data = {"log_error": True, **log_context([], page)}
    return await render_page(
        request,
        context,
        session,
        "pages/promotion_log.html",
        "Promotion log",
        path="/strategies",
        data=data,
    )


@router.get("/strategies/{study_id}")
async def strategy_detail(request: Request, session: SessionDep, study_id: UUID) -> Response:
    """One study in depth (spec 0015 AC-19): folds, bounds, benchmarks, trials, every attempt; 404 when unknown."""
    context = app_context(request)
    detail = await context.read_study(study_id)
    if detail is None:
        raise HTTPException(status_code=404)
    return await render_page(
        request,
        context,
        session,
        "pages/strategy_detail.html",
        detail.ranking.study,
        path="/strategies",
        data=detail_context(detail),
    )


@router.get("/system")
async def system(request: Request, session: SessionDep) -> Response:
    context = app_context(request)
    return await render_page(request, context, session, *PAGES["/system"])


@router.get("/styleguide")
async def styleguide(request: Request, session: SessionDep) -> Response:
    context = app_context(request)
    return await render_page(request, context, session, *PAGES["/styleguide"])


def sse_message(event: str, data: str) -> str:
    """One SSE message; every line of ``data`` gets its own ``data:`` prefix."""
    lines = "".join(f"data: {line}\n" for line in (data.splitlines() or [""]))
    return f"event: {event}\n{lines}\n"


async def event_stream(
    poller: Poller,
    initial: Mapping[str, str],
    ends_at: float,
    clock: Callable[[], float],
    monotonic: Callable[[], float] = time.monotonic,
    ping_every_s: float = PING_EVERY_S,
) -> AsyncIterator[str]:
    """The current partials at once, then each change, plus ``ping`` every 2 s, until ``ends_at``.

    The stream ends when the session would expire; the browser reconnects and gets 204.
    """
    queue: asyncio.Queue[Event] = poller.subscribe()
    try:
        for name, html in initial.items():
            yield sse_message(name, html)
        next_ping = monotonic() + ping_every_s
        while True:
            remaining = ends_at - clock()
            if remaining <= 0:
                return
            try:
                name, html = await asyncio.wait_for(
                    queue.get(), timeout=max(min(next_ping - monotonic(), remaining), 0)
                )
                yield sse_message(name, html)
            except TimeoutError:
                pass
            if monotonic() >= next_ping:
                yield sse_message("ping", "")
                next_ping = monotonic() + ping_every_s
    finally:
        poller.unsubscribe(queue)


@router.get("/events")
async def events(request: Request, session: SessionDep) -> Response:
    context = app_context(request)
    initial = context.poller.cached or await context.poller.render_now()
    return StreamingResponse(
        event_stream(context.poller, initial, context.codec.ends_at(session), context.clock),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )

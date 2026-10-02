"""The shell pages and the live value stream (spec 0004 AC-7, AC-9).

Milestone 1 serves Overview only; the other nav pages, the stop button, and the command
state arrive with the later milestones.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from markupsafe import Markup

from dashboard.auth.session import Session
from dashboard.live.poller import Event, Poller
from dashboard.web.context import AppContext, app_context
from dashboard.web.session import SessionDep, session_end_cue

router = APIRouter()

PING_EVERY_S: Final = 2.0


async def render_page(request: Request, context: AppContext, session: Session, template: str, title: str) -> Response:
    """A page inside the shell, its live partials read from Redis right now."""
    partials = await context.poller.render_now()
    return context.templates.TemplateResponse(
        request,
        template,
        {
            "env": context.settings.trading_env,
            "title": title,
            "csrf": session.csrf,
            "session_end": session_end_cue(context, session),
            # Rendered by our own autoescaping templates.
            "live": {name: Markup(html) for name, html in partials.items()},
        },
    )


@router.get("/")
async def overview(request: Request, session: SessionDep) -> Response:
    context = app_context(request)
    return await render_page(request, context, session, "pages/overview.html", "Overview")


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

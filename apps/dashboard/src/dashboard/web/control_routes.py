"""The stop button (spec 0004 AC-11, AC-13): one tap appends one ``stop`` to ``{env}:control``.

The button is a plain form that HTMX enhances, so it works without JavaScript. It depends
only on the session, the CSRF token, and Redis; it never shows ``sent`` unless the append
succeeded.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from redis.typing import EncodableT, FieldT

from dashboard.auth.next_path import referer_path
from dashboard.auth.session import Session
from dashboard.live.poller import COMMAND_EVENT, REDIS_ERRORS
from dashboard.web.context import AppContext, app_context
from dashboard.web.page_routes import render_referring_page
from dashboard.web.session import STOP_PATH, CsrfDep, current_session, handle_csrf_refused, is_htmx
from trading_core.control.messages import control_stream, stop_command, to_fields

router = APIRouter()

log = logging.getLogger(__name__)

# The control stream keeps about this many commands (XADD MAXLEN ~).
STREAM_MAXLEN: Final = 1000
SEND_TIMEOUT_S: Final = 1.0
NOT_SENT: Final = "Stop NOT sent: Redis did not answer"
NOT_SENT_CSRF: Final = "Stop NOT sent: reload the page"


def render_stop_area(context: AppContext, session: Session, error: str | None, status_code: int) -> Response:
    """The stop area partial for HTMX, plus the command state as an out of band swap when sent."""
    stop = context.templates.get_template("partials/stop_area.html").render(csrf=session.csrf, error=error)
    command = "" if error else context.poller.cached.get(COMMAND_EVENT, "")
    html = stop
    if command:
        html += f'<span id="{COMMAND_EVENT}" hx-swap-oob="innerHTML">{command}</span>'
    return Response(html, status_code=status_code, media_type="text/html")


async def stop_failed(
    request: Request, context: AppContext, session: Session, error: str, status_code: int
) -> Response:
    """``Stop NOT sent``: the stop area for HTMX, the referring page with that state for a plain form.

    After a failed send the page shows ``link lost`` without reading Redis again, so it answers
    within the send's own deadline (AC-13).
    """
    if is_htmx(request):
        return render_stop_area(context, session, error, status_code)
    live = context.poller.link_lost() if error == NOT_SENT else None
    return await render_referring_page(request, context, session, stop_error=error, status_code=status_code, live=live)


@router.post(STOP_PATH)
async def stop(request: Request, session: CsrfDep) -> Response:
    context = app_context(request)
    msg = stop_command(datetime.fromtimestamp(context.clock(), tz=UTC), context.settings.control_command_ttl_s)
    fields: dict[FieldT, EncodableT] = {name: value for name, value in to_fields(msg).items()}
    log_fields = {"id": str(msg.id), "cmd": msg.cmd.value}
    try:
        await asyncio.wait_for(
            context.redis.xadd(control_stream(context.settings.trading_env), fields, maxlen=STREAM_MAXLEN),
            timeout=SEND_TIMEOUT_S,
        )
    except REDIS_ERRORS as exc:
        log.error(
            "stop not sent",
            extra={"fields": {"code": "control_send_failed", **log_fields, "error": type(exc).__name__}},
        )
        return await stop_failed(request, context, session, NOT_SENT, 503)
    log.info("control command sent", extra={"fields": {"code": "control_sent", **log_fields}})
    context.poller.note_sent(msg)
    if is_htmx(request):
        return render_stop_area(context, session, None, 200)
    return RedirectResponse(referer_path(request.headers.get("referer")), status_code=303)


async def handle_csrf_refused_on_stop(request: Request, exc: Exception) -> Response:
    """403 for a stop without the right CSRF token: nothing was appended, and the stop area says so.

    Any other POST keeps the plain ``handle_csrf_refused`` reply.
    """
    session = current_session(request)
    if request.url.path != STOP_PATH or session is None:
        return await handle_csrf_refused(request, exc)
    return await stop_failed(request, app_context(request), session, NOT_SENT_CSRF, 403)

"""The control buttons: Stop in the top bar (spec 0004 AC-11, AC-13), Pause, Resume, Flatten, and Recheck
on System (spec 0005 AC-13, spec 0010 AC-18, spec 0023 AC-20). One tap appends one command to ``{env}:control``.

Each button is a plain form that HTMX enhances, so it works without JavaScript. It depends
only on the session, the CSRF token, and Redis; it never shows ``sent`` unless the append
succeeded. Stop stays one tap. Resume (``resume``: it lifts a halt and clears any pause, whoever
set it) and Flatten take a confirm step: a POST without ``confirm=1`` only renders the area in
its confirm state, and the confirm form's POST carries ``confirm=1`` and sends.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from redis.typing import EncodableT, FieldT

from dashboard.auth.next_path import referer_page
from dashboard.auth.session import Session
from dashboard.live.poller import COMMAND_EVENT, REDIS_ERRORS
from dashboard.web.context import AppContext, app_context
from dashboard.web.page_routes import render_referring_page
from dashboard.web.session import (
    FLATTEN_PATH,
    PAUSE_PATH,
    RECHECK_PATH,
    RESUME_PATH,
    STOP_PATH,
    CsrfDep,
    current_session,
    handle_csrf_refused,
    is_htmx,
)
from trading_core.control.messages import ControlMessage, command, control_stream, stop_command, to_fields
from trading_core.recording.enums import ControlCmd, ControlSource

router = APIRouter()

log = logging.getLogger(__name__)

# The control stream keeps about this many commands (XADD MAXLEN ~).
STREAM_MAXLEN: Final = 1000
SEND_TIMEOUT_S: Final = 1.0


@dataclass(frozen=True)
class Button:
    """One control button: its area partial, the page key for its error, and its NOT sent words.

    ``confirm`` names the button's confirm step (``resume``, ``flatten``); None sends on one tap.
    """

    area: str
    template: str
    error_key: str
    not_sent: str
    not_sent_csrf: str
    confirm: str | None = None


STOP: Final = Button(
    "stop-area",
    "partials/stop_area.html",
    "stop_error",
    "Stop NOT sent: Redis did not answer",
    "Stop NOT sent: reload the page",
)
PAUSE: Final = Button(
    "pause-area",
    "partials/pause_area.html",
    "pause_error",
    "Pause NOT sent: Redis did not answer",
    "Pause NOT sent: reload the page",
)
RESUME: Final = Button(
    "pause-area",
    "partials/pause_area.html",
    "pause_error",
    "Resume NOT sent: Redis did not answer",
    "Resume NOT sent: reload the page",
    confirm="resume",
)
FLATTEN: Final = Button(
    "pause-area",
    "partials/pause_area.html",
    "pause_error",
    "Flatten NOT sent: Redis did not answer",
    "Flatten NOT sent: reload the page",
    confirm="flatten",
)
RECHECK: Final = Button(
    "pause-area",
    "partials/pause_area.html",
    "pause_error",
    "Recheck NOT sent: Redis did not answer",
    "Recheck NOT sent: reload the page",
)
BUTTONS: Final = {
    STOP_PATH: STOP,
    PAUSE_PATH: PAUSE,
    RESUME_PATH: RESUME,
    FLATTEN_PATH: FLATTEN,
    RECHECK_PATH: RECHECK,
}
CONFIRMED: Final = "1"


def render_area(
    context: AppContext,
    session: Session,
    button: Button,
    error: str | None,
    status_code: int,
    confirm: str | None = None,
) -> Response:
    """The button's area partial for HTMX, plus the command state as an out of band swap when sent."""
    area = context.templates.get_template(button.template).render(csrf=session.csrf, error=error, confirm=confirm)
    command_html = "" if error or confirm else context.poller.cached.get(COMMAND_EVENT, "")
    html = area
    if command_html:
        html += f'<span id="{COMMAND_EVENT}" hx-swap-oob="innerHTML">{command_html}</span>'
    return Response(html, status_code=status_code, media_type="text/html")


async def send_failed(
    request: Request, context: AppContext, session: Session, button: Button, error: str, status_code: int
) -> Response:
    """``... NOT sent``: the area for HTMX, the referring page with that state for a plain form.

    After a failed send the page shows ``no data`` without reading Redis again, so it answers
    within the send's own deadline (spec 0004 AC-13).
    """
    if is_htmx(request):
        return render_area(context, session, button, error, status_code)
    live = context.poller.no_data() if error == button.not_sent else None
    return await render_referring_page(
        request, context, session, errors={button.error_key: error}, status_code=status_code, live=live
    )


async def send(request: Request, session: Session, button: Button, msg: ControlMessage) -> Response:
    """Append ``msg`` within ``SEND_TIMEOUT_S``; on success show it as sent, else ``NOT sent``."""
    context = app_context(request)
    fields: dict[FieldT, EncodableT] = {name: value for name, value in to_fields(msg).items()}
    log_fields = {"id": str(msg.id), "cmd": msg.cmd.value}
    try:
        await asyncio.wait_for(
            context.redis.xadd(control_stream(context.settings.trading_env), fields, maxlen=STREAM_MAXLEN),
            timeout=SEND_TIMEOUT_S,
        )
    except REDIS_ERRORS as exc:
        log.error(
            f"{msg.cmd.value} not sent",
            extra={"fields": {"code": "control_send_failed", **log_fields, "error": type(exc).__name__}},
        )
        return await send_failed(request, context, session, button, button.not_sent, 503)
    log.info("control command sent", extra={"fields": {"code": "control_sent", **log_fields}})
    context.poller.note_sent(msg)
    if is_htmx(request):
        return render_area(context, session, button, None, 200)
    return RedirectResponse(referer_page(request.headers.get("referer")), status_code=303)


def _now(context: AppContext) -> datetime:
    return datetime.fromtimestamp(context.clock(), tz=UTC)


@router.post(STOP_PATH)
async def stop(request: Request, session: CsrfDep) -> Response:
    context = app_context(request)
    msg = stop_command(_now(context), context.settings.control_command_ttl_s)
    return await send(request, session, STOP, msg)


@router.post(PAUSE_PATH)
async def pause(request: Request, session: CsrfDep) -> Response:
    context = app_context(request)
    msg = command(ControlCmd.PAUSE, _now(context), context.settings.control_command_ttl_s, ControlSource.DASHBOARD)
    return await send(request, session, PAUSE, msg)


@router.post(RECHECK_PATH)
async def recheck(request: Request, session: CsrfDep) -> Response:
    # Only reads Kraken and reports, so it sends on one tap (spec 0023 AC-20).
    context = app_context(request)
    msg = command(ControlCmd.RECHECK, _now(context), context.settings.control_command_ttl_s, ControlSource.DASHBOARD)
    return await send(request, session, RECHECK, msg)


async def ask_confirm(request: Request, session: Session, button: Button) -> Response:
    """The button's confirm step: nothing is sent, the area asks first (spec 0010 AC-18)."""
    context = app_context(request)
    if is_htmx(request):
        return render_area(context, session, button, None, 200, confirm=button.confirm)
    return await render_referring_page(request, context, session, status_code=200, confirm=button.confirm)


async def confirmed_send(
    request: Request, session: Session, button: Button, cmd: ControlCmd, confirm: str | None
) -> Response:
    """Send ``cmd`` when the POST carries ``confirm=1``, else render the confirm step."""
    if confirm != CONFIRMED:
        return await ask_confirm(request, session, button)
    context = app_context(request)
    msg = command(cmd, _now(context), context.settings.control_command_ttl_s, ControlSource.DASHBOARD)
    return await send(request, session, button, msg)


@router.post(RESUME_PATH)
async def resume(request: Request, session: CsrfDep, confirm: Annotated[str | None, Form()] = None) -> Response:
    return await confirmed_send(request, session, RESUME, ControlCmd.RESUME, confirm)


@router.post(FLATTEN_PATH)
async def flatten(request: Request, session: CsrfDep, confirm: Annotated[str | None, Form()] = None) -> Response:
    return await confirmed_send(request, session, FLATTEN, ControlCmd.FLATTEN, confirm)


async def handle_csrf_refused_on_stop(request: Request, exc: Exception) -> Response:
    """403 for a control button without the right CSRF token: nothing was appended, and its area says so.

    Any other POST keeps the plain ``handle_csrf_refused`` reply.
    """
    session = current_session(request)
    button = BUTTONS.get(request.url.path)
    if button is None or session is None:
        return await handle_csrf_refused(request, exc)
    return await send_failed(request, app_context(request), session, button, button.not_sent_csrf, 403)

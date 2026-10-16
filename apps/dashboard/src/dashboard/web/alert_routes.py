"""The alert controls on Overview (spec 0013 AC-12, AC-16): Mute warnings for 1, 4, or 12 h, Unmute,
and Send test alert. Each tap appends one command to ``{env}:control:alerts`` for the alerter.

Like Pause, each is a plain form that HTMX enhances and never shows ``sent`` unless the append
succeeded. The badge follows the alerter's answer on the results stream (the poller reads it).
The test waits up to ``TEST_WAIT_S`` for the alerter's answer and says what it was.
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from redis.typing import EncodableT, FieldT

from dashboard.auth.next_path import referer_page
from dashboard.auth.session import Session
from dashboard.live.alerts import answer_of
from dashboard.live.poller import REDIS_ERRORS
from dashboard.web import control_routes
from dashboard.web.context import AppContext, app_context
from dashboard.web.page_routes import render_referring_page
from dashboard.web.session import ALERTS_MUTE_PATH, ALERTS_TEST_PATH, CsrfDep, current_session, is_htmx
from trading_core.control.alerts import (
    AlertCommand,
    alert_command,
    alerts_results_stream,
    alerts_stream,
    command_to_fields,
)
from trading_core.recording.enums import AlertCmd, ControlOutcome

router = APIRouter()

log = logging.getLogger(__name__)

TEMPLATE: Final = "partials/alerts_area.html"
# Mute lengths the control offers, in hours; 0 is Unmute.
MUTE_HOURS: Final = frozenset({0, 1, 4, 12})
TEST_WAIT_S: Final = 10.0
TEST_POLL_S: Final = 0.25
RESULTS_SEARCHED: Final = 200
NOT_SENT_REDIS: Final = "NOT sent: Redis did not answer"
NOT_SENT_CSRF: Final = "NOT sent: reload the page"
NO_ANSWER: Final = "Test sent, but no answer from the alerter within 10 s"
_REASONS: Final = {"push_failed": "a push failed, see the Activity feed", "invalid": "the alerter refused it"}


def render_area(
    context: AppContext, session: Session, *, error: str | None, note: str | None, status_code: int
) -> Response:
    """The alert controls partial for HTMX."""
    html = context.templates.get_template(TEMPLATE).render(csrf=session.csrf, error=error, note=note)
    return Response(html, status_code=status_code, media_type="text/html")


async def answer(
    request: Request, session: Session, *, error: str | None, note: str | None, status_code: int
) -> Response:
    """The area for HTMX; for a plain form, the referring page with that state."""
    context = app_context(request)
    if is_htmx(request):
        return render_area(context, session, error=error, note=note, status_code=status_code)
    errors = {"alerts_error": error} if error else {"alerts_note": note or ""}
    live = context.poller.no_data() if error == NOT_SENT_REDIS else None
    return await render_referring_page(request, context, session, errors=errors, status_code=status_code, live=live)


def _now(context: AppContext) -> datetime:
    return datetime.fromtimestamp(context.clock(), tz=UTC)


async def _append(context: AppContext, msg: AlertCommand) -> bool:
    """Append ``msg`` within the control send timeout; False (logged) when Redis did not take it."""
    fields: dict[FieldT, EncodableT] = {name: value for name, value in command_to_fields(msg).items()}
    log_fields = {"id": str(msg.id), "cmd": msg.cmd.value}
    try:
        await asyncio.wait_for(
            context.redis.xadd(
                alerts_stream(context.settings.trading_env), fields, maxlen=control_routes.STREAM_MAXLEN
            ),
            timeout=control_routes.SEND_TIMEOUT_S,
        )
    except REDIS_ERRORS as exc:
        log.error(
            f"{msg.cmd.value} not sent",
            extra={"fields": {"code": "alert_command_send_failed", **log_fields, "error": type(exc).__name__}},
        )
        return False
    log.info("alert command sent", extra={"fields": {"code": "alert_command_sent", **log_fields}})
    return True


@router.post(ALERTS_MUTE_PATH)
async def mute(request: Request, session: CsrfDep, hours: Annotated[int, Form()]) -> Response:
    """Mute warnings for ``hours`` (1, 4, or 12), or unmute with 0; 422 for any other number."""
    if hours not in MUTE_HOURS:
        raise HTTPException(status_code=422, detail="hours must be 1, 4, 12, or 0")
    context = app_context(request)
    ttl_s = context.settings.control_command_ttl_s
    if hours:
        msg = alert_command(AlertCmd.MUTE, _now(context), ttl_s, mute_s=hours * 3600)
        label = f"Mute for {hours} h"
    else:
        msg = alert_command(AlertCmd.UNMUTE, _now(context), ttl_s)
        label = "Unmute"
    if not await _append(context, msg):
        return await answer(request, session, error=f"{label} {NOT_SENT_REDIS}", note=None, status_code=503)
    if not is_htmx(request):
        return RedirectResponse(referer_page(request.headers.get("referer")), status_code=303)
    return await answer(request, session, error=None, note=f"{label} sent", status_code=200)


@router.post(ALERTS_TEST_PATH)
async def test(request: Request, session: CsrfDep) -> Response:
    """Ask the alerter for one marked test push per tier and wait up to ``TEST_WAIT_S`` for its answer."""
    context = app_context(request)
    msg = alert_command(AlertCmd.TEST, _now(context), context.settings.control_command_ttl_s)
    if not await _append(context, msg):
        return await answer(request, session, error=f"Test {NOT_SENT_REDIS}", note=None, status_code=503)
    return await answer(request, session, error=None, note=await _test_answer(context, msg.id), status_code=200)


async def _test_answer(context: AppContext, command_id: uuid.UUID) -> str:
    """What the alerter answered the test ``command_id``, in plain words."""
    stream = alerts_results_stream(context.settings.trading_env)
    deadline = time.monotonic() + TEST_WAIT_S
    while True:
        try:
            result = answer_of(await context.redis.xrevrange(stream, count=RESULTS_SEARCHED), command_id)
        except REDIS_ERRORS:
            result = None
        if result is not None:
            if result.outcome is ControlOutcome.EXECUTED:
                return "Test sent: one push per tier"
            if result.outcome is ControlOutcome.EXPIRED:
                return "Test expired before the alerter read it"
            return f"Test failed: {_REASONS.get(result.reason or '', result.reason or 'unknown reason')}"
        if time.monotonic() >= deadline:
            return NO_ANSWER
        await asyncio.sleep(TEST_POLL_S)


async def handle_csrf_refused(request: Request, exc: Exception) -> Response:
    """403 for an alert control without the right CSRF token: its area says NOT sent.

    Any other POST goes to the stop's handler.
    """
    session = current_session(request)
    if request.url.path not in (ALERTS_MUTE_PATH, ALERTS_TEST_PATH) or session is None:
        return await control_routes.handle_csrf_refused_on_stop(request, exc)
    return await answer(request, session, error=NOT_SENT_CSRF, note=None, status_code=403)

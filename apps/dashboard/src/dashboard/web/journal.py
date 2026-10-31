"""The journal export routes (spec 0024): the Check panel, ``journal.csv``, ``summary.csv``, and the form's defaults.

The three parameters arrive as plain strings and are validated in ``trading_core.journal.bounds``,
so a bad value gets our own text, never FastAPI's JSON 422. The CSVs answer a bad range with 422,
a refusal with 409, and no Postgres with 503, each as plain text; every refusal is decided
before the first byte, since a 409 cannot be sent once a stream has started. The Check panel
always answers 200 so HTMX swaps it (it skips 4xx and 5xx), showing each of those as a state.
"""

import io
import logging
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError

from dashboard.data.journal import JournalExport
from dashboard.web.context import AppContext, app_context
from dashboard.web.session import SessionDep
from trading_core.journal.bounds import (
    EXPORT_MODES,
    JournalRange,
    JournalRangeError,
    amsterdam_today,
    default_dates,
    default_mode,
    parse_range,
)
from trading_core.journal.export import JournalRefusedError, journal_filename
from trading_core.journal.summary import JournalSummary, write_summary

router = APIRouter()
log = logging.getLogger(__name__)

CSV_TYPE: Final = "text/csv; charset=utf-8"
PANEL: Final = "partials/journal_panel.html"
NO_POSTGRES: Final = "The dashboard cannot read Postgres right now. Try again in a moment."
Param = Annotated[str | None, Query()]


def today(context: AppContext) -> date:
    """Today in Europe/Amsterdam by the server clock."""
    return amsterdam_today(datetime.fromtimestamp(context.clock(), UTC))


def journal_form(context: AppContext) -> dict[str, object]:
    """The export form's defaults (AC-1): 1 January to yesterday, and the env's own mode."""
    start, end = default_dates(today(context))
    return {
        "journal_form": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "mode": default_mode(context.settings.trading_env).value,
            "modes": [m.value for m in EXPORT_MODES],
        }
    }


def _fields(context: AppContext, journal_range: JournalRange, file: str) -> dict[str, object]:
    return {
        "env": context.settings.trading_env,
        "mode": journal_range.mode.value,
        "start": journal_range.start.isoformat(),
        "end": journal_range.end.isoformat(),
        "file": file,
    }


def _refused(reason: str, message: str, status_code: int) -> Response:
    log.info("journal export refused", extra={"fields": {"code": "journal_export_refused", "reason": reason}})
    return PlainTextResponse(message, status_code=status_code)


def _stream(export: JournalExport, fields: dict[str, object]) -> Iterator[str]:
    yield from export.chunks()
    log.info("journal exported", extra={"fields": {"code": "journal_exported", **fields, "rows": export.rows}})


def _read_failed(exc: SQLAlchemyError) -> None:
    log.warning("journal read failed", extra={"fields": {"code": "journal_read_failed", "error": type(exc).__name__}})


async def _opened[T](
    context: AppContext, params: tuple[str | None, str | None, str | None], read: Callable[[JournalRange], Awaitable[T]]
) -> tuple[JournalRange, T] | Response:
    """The validated range and what ``read`` gave for it, or the 422, 409, or 503 answer."""
    try:
        journal_range = parse_range(*params, today(context))
    except JournalRangeError as exc:
        return _refused(exc.reason, str(exc), 422)
    try:
        return journal_range, await read(journal_range)
    except JournalRefusedError as exc:
        return _refused(exc.reason, str(exc), 409)
    except SQLAlchemyError as exc:
        _read_failed(exc)
        return PlainTextResponse(NO_POSTGRES, 503)


def _attachment(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"'}


@router.get("/trades/journal/check")
async def journal_check(
    request: Request, session: SessionDep, start: Param = None, end: Param = None, mode: Param = None
) -> Response:
    """The summary panel (AC-7 to AC-9) for HTMX: always 200, a refusal or an error is a panel state.

    Once the range passes, the panel links both CSVs with the same three parameters (AC-1).
    """
    context = app_context(request)
    panel: dict[str, object] = {"summary": None, "message": None, "state": "refused"}
    try:
        journal_range = parse_range(start, end, mode, today(context))
        summary = await context.read_journal_summary(journal_range)
    except (JournalRangeError, JournalRefusedError) as exc:
        panel["message"] = str(exc)
    except SQLAlchemyError as exc:
        _read_failed(exc)
        panel.update(state="error", message=NO_POSTGRES)
    else:
        query = urlencode({"start": journal_range.start, "end": journal_range.end, "mode": journal_range.mode.value})
        panel.update(state=summary.check.status.value, summary=summary, query=query)
    html = context.templates.get_template(PANEL).render(**panel)
    return HTMLResponse(html)


@router.get("/trades/journal.csv")
async def journal_csv(
    request: Request, session: SessionDep, start: Param = None, end: Param = None, mode: Param = None
) -> Response:
    """Every fill, fee, and funding row of the range as CSV (AC-3, AC-4), streamed one month at a time."""
    context = app_context(request)
    opened = await _opened(context, (start, end, mode), context.open_journal)
    if isinstance(opened, Response):
        return opened
    journal_range, export = opened
    return StreamingResponse(
        _stream(export, _fields(context, journal_range, "journal")),
        media_type=CSV_TYPE,
        headers=_attachment(journal_filename(context.settings.trading_env, journal_range)),
    )


@router.get("/trades/journal-summary.csv")
async def journal_summary_csv(
    request: Request, session: SessionDep, start: Param = None, end: Param = None, mode: Param = None
) -> Response:
    """The range's totals, balances, and P&L check as ``section,item,currency,value`` rows (AC-10)."""
    context = app_context(request)
    opened: tuple[JournalRange, JournalSummary] | Response = await _opened(
        context, (start, end, mode), context.read_journal_summary
    )
    if isinstance(opened, Response):
        return opened
    journal_range, summary = opened
    env = context.settings.trading_env
    buffer = io.StringIO()
    generated_at = datetime.fromtimestamp(context.clock(), UTC)
    write_summary(buffer, env, summary, generated_at, context.settings.git_commit)
    fields = {**_fields(context, journal_range, "summary"), "rows": summary.rows}
    log.info("journal exported", extra={"fields": {"code": "journal_exported", **fields}})
    return Response(
        buffer.getvalue(), media_type=CSV_TYPE, headers=_attachment(journal_filename(env, journal_range, "summary"))
    )

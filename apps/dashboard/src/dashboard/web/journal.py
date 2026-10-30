"""The journal export routes (spec 0024): ``/trades/journal.csv`` and the form's defaults on ``/trades``.

The three parameters arrive as plain strings and are validated in ``trading_core.journal.bounds``,
so a bad value gets our own 422 text, never FastAPI's JSON. Every refusal is decided before the
first byte: a 409 cannot be sent once a stream has started.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
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

router = APIRouter()
log = logging.getLogger(__name__)

CSV_TYPE: Final = "text/csv; charset=utf-8"
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


@router.get("/trades/journal.csv")
async def journal_csv(
    request: Request, session: SessionDep, start: Param = None, end: Param = None, mode: Param = None
) -> Response:
    """Every fill, fee, and funding row of the range as CSV (AC-3, AC-4).

    422 for a bad range, 409 when refused, 503 without Postgres, each as plain text.
    """
    context = app_context(request)
    try:
        journal_range = parse_range(start, end, mode, today(context))
    except JournalRangeError as exc:
        return _refused(exc.reason, str(exc), 422)
    try:
        export = await context.open_journal(journal_range)
    except JournalRefusedError as exc:
        return _refused(exc.reason, str(exc), 409)
    except SQLAlchemyError as exc:
        log.warning(
            "journal read failed", extra={"fields": {"code": "journal_read_failed", "error": type(exc).__name__}}
        )
        return PlainTextResponse("The dashboard cannot read Postgres right now. Try again in a moment.", 503)
    env = context.settings.trading_env
    filename = journal_filename(env, journal_range)
    return StreamingResponse(
        _stream(export, _fields(context, journal_range, "journal")),
        media_type=CSV_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

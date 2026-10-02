"""The session dependency and what happens without a session (spec 0004 AC-1)."""

import hmac
from datetime import UTC, datetime
from typing import Annotated, Final
from urllib.parse import urlencode

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse, Response

from dashboard.auth.next_path import referer_path, safe_next
from dashboard.auth.session import COOKIE_NAME, Session
from dashboard.web.context import AppContext, app_context

EVENTS_PATH: Final = "/events"
STOP_PATH: Final = "/control/stop"
CSRF_HEADER: Final = "x-csrf-token"
CSRF_FIELD: Final = "csrf"
# Show the "session ends" cue in the top bar this long before the session expires.
SESSION_END_CUE_S: Final = 3600


class LoginRequired(Exception):
    """The request needs a valid session and has none."""


class CsrfRefused(Exception):
    """A POST without the session's CSRF token."""


def current_session(request: Request) -> Session | None:
    """The valid session in the request's cookie, or None."""
    context = app_context(request)
    return context.codec.decode(request.cookies.get(COOKIE_NAME), context.clock())


def require_session(request: Request) -> Session:
    """Dependency: the valid session, else ``LoginRequired``.

    Every authenticated response except ``/events`` re-signs the cookie with a new ``seen``
    (``ShellMiddleware`` reads it from ``request.state``), so an open SSE stream never keeps a
    session alive.
    """
    session = current_session(request)
    if session is None:
        raise LoginRequired
    if request.url.path != EVENTS_PATH:
        request.state.session = session
    return session


SessionDep = Annotated[Session, Depends(require_session)]


async def require_csrf(request: Request, session: SessionDep) -> Session:
    """Dependency for every POST: the session, else ``CsrfRefused`` when the token is missing or wrong.

    The token comes from the ``X-CSRF-Token`` header (HTMX) or the ``csrf`` form field (plain form).
    """
    sent = request.headers.get(CSRF_HEADER)
    if sent is None:
        form = await request.form()
        field = form.get(CSRF_FIELD)
        sent = field if isinstance(field, str) else None
    if sent is None or not hmac.compare_digest(sent.encode(), session.csrf.encode()):
        raise CsrfRefused
    return session


CsrfDep = Annotated[Session, Depends(require_csrf)]


def is_htmx(request: Request) -> bool:
    """Whether HTMX sent ``request`` (it then swaps a partial instead of loading a page)."""
    return request.headers.get("hx-request") == "true"


def login_redirect_target(request: Request) -> str:
    """``/login?next=<path>``: the page itself for a GET, the referring page for a POST.

    A stop tap without a session adds ``stop_lost=1``, so the login page says the stop was NOT sent.
    """
    path = request.url.path if request.method == "GET" else referer_path(request.headers.get("referer"))
    query = {"next": safe_next(path)}
    if request.url.path == STOP_PATH:
        query["stop_lost"] = "1"
    return "/login?" + urlencode(query)


async def handle_login_required(request: Request, exc: Exception) -> Response:
    """303 to the login page; 401 with ``HX-Redirect`` for HTMX; 204 for ``/events`` so EventSource stops."""
    if request.url.path == EVENTS_PATH:
        return Response(status_code=204)
    target = login_redirect_target(request)
    if is_htmx(request):
        return Response(status_code=401, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


async def handle_csrf_refused(request: Request, exc: Exception) -> Response:
    """403; nothing was done."""
    return Response("CSRF token missing or wrong: reload the page", status_code=403, media_type="text/plain")


def session_end_cue(context: AppContext, session: Session) -> str | None:
    """``HH:MMZ`` when the session (after this request) ends within the hour, else None."""
    ends_at = context.codec.ends_at(context.codec.touch(session, context.clock()))
    if ends_at - context.clock() >= SESSION_END_CUE_S:
        return None
    return datetime.fromtimestamp(ends_at, tz=UTC).strftime("%H:%MZ")

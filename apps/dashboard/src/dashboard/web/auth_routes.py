"""Login and logout (spec 0004 AC-1 to AC-3)."""

import asyncio
import ipaddress
import logging
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from dashboard.auth.next_path import safe_next
from dashboard.web.context import AppContext, app_context
from dashboard.web.middleware import clear_cookie, session_cookie
from dashboard.web.session import CsrfDep, current_session

log = logging.getLogger(__name__)

router = APIRouter()

WRONG: Final = "Password or code is wrong"
LOCKED: Final = "Too many attempts. Try again in 15 minutes."
STOP_LOST: Final = "Your stop was NOT sent. Log in and tap stop again."
# A Pause, Resume, or Flatten tap whose session had expired (spec 0005 AC-13, spec 0010 AC-18); keyed by
# the `lost` query value.
LOST_NOTICES: Final = {
    "pause": "Your pause was NOT sent. Log in and tap Pause again.",
    "resume": "Your resume was NOT sent. Log in and tap Resume again.",
    "flatten": "Your flatten was NOT sent. Log in and tap Flatten again.",
}

Reason = Literal["bad_password", "bad_code", "replayed_code", "locked"]


def lockout_key(request: Request, trusted_peers: frozenset[str] = frozenset()) -> str:
    """Who is trying, for the lockout only (never for access).

    When the socket peer is loopback or a trusted peer (the container's default gateway, where
    Docker's published port delivers ``tailscale serve``), the Tailscale login header, else the
    first ``X-Forwarded-For`` address, names the client; otherwise the peer address does.
    """
    peer = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        is_loopback = False
    if is_loopback or peer in trusted_peers:
        user = request.headers.get("tailscale-user-login", "").strip()
        if user:
            return user
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return peer or "unknown"


def _login_page(
    request: Request,
    context: AppContext,
    next_path: str,
    *,
    error: str | None = None,
    stop_lost: bool = False,
    lost: str | None = None,
    status_code: int = 200,
) -> Response:
    return context.templates.TemplateResponse(
        request,
        "login.html",
        {
            "env": context.settings.trading_env,
            "title": "Log in",
            "next": next_path,
            "error": error,
            "stop_lost": stop_lost,
            "lost_notice": LOST_NOTICES.get(lost or ""),
        },
        status_code=status_code,
    )


@router.get("/login")
async def login_form(
    request: Request, next: str = "/", stop_lost: str | None = None, lost: str | None = None
) -> Response:
    context = app_context(request)
    if current_session(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _login_page(request, context, safe_next(next), stop_lost=stop_lost is not None, lost=lost)


@router.post("/login")
async def login(
    request: Request,
    password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    context = app_context(request)
    key = lockout_key(request, context.trusted_peers)
    target = safe_next(next)
    # One attempt at a time: a burst cannot run many 64 MiB password checks at once, and each
    # attempt sees the failures the ones before it recorded.
    async with context.totp.lock:
        now = context.clock()
        if context.lockout.is_locked(key, now):
            _log_failure("locked", key)
            return _login_page(request, context, target, error=LOCKED, status_code=429)

        # Both factors are evaluated on every attempt, so the answer never reveals which one failed.
        password_ok = await asyncio.to_thread(context.passwords.verify, password)
        step = context.totp.matched_step(code.strip(), now)
        reason: Reason | None = None
        if not password_ok:
            reason = "bad_password"
        elif step is None:
            reason = "bad_code"
        elif context.totp.is_replay(step):
            reason = "replayed_code"
        else:
            context.totp.accept(step)

        if reason is not None:
            context.lockout.record_failure(key, now)
            _log_failure(reason, key)
            return _login_page(request, context, target, error=WRONG)

        context.lockout.record_success(key)
    session = context.codec.new(now)
    log.info("login ok", extra={"fields": {"code": "login_ok", "client": key}})
    response = RedirectResponse(target, status_code=303)
    max_age = int(context.codec.ends_at(session) - now)
    response.headers.append(
        "set-cookie", session_cookie(context.codec.encode(session), max_age, context.settings.dashboard_cookie_secure)
    )
    return response


@router.post("/logout")
async def logout(request: Request, session: CsrfDep) -> Response:
    context = app_context(request)
    log.info("logout", extra={"fields": {"code": "logout"}})
    response = RedirectResponse("/login", status_code=303)
    response.headers.append("set-cookie", clear_cookie(context.settings.dashboard_cookie_secure))
    return response


def _log_failure(reason: Reason, key: str) -> None:
    # Never the submitted password or code (spec 0004 AC-2).
    log.warning("login failed", extra={"fields": {"code": "login_failed", "reason": reason, "client": key}})

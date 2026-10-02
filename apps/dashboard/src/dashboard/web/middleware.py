"""Response headers and the sliding session cookie, as one plain ASGI middleware (spec 0004 AC-4, AC-18).

A plain ASGI middleware (not ``BaseHTTPMiddleware``) so the SSE stream passes through untouched.
"""

from collections.abc import Callable
from typing import Final

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from dashboard.auth.session import COOKIE_NAME, Session, SessionCodec

SECURITY_HEADERS: Final = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    # Our own origin only, so a POST after an expired session knows its page (spec 0004 AC-1).
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}
STATIC_CACHE: Final = "public, max-age=86400"


def session_cookie(value: str, max_age: int, secure: bool) -> str:
    """A ``Set-Cookie`` value for the session: HttpOnly, SameSite Strict, whole site."""
    flags = f"{COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Strict"
    return f"{flags}; Secure" if secure else flags


def clear_cookie(secure: bool) -> str:
    """A ``Set-Cookie`` value that deletes the session cookie."""
    return session_cookie("", 0, secure)


class ShellMiddleware:
    """Adds the security headers to every HTML response and re-signs the session cookie.

    A handler that sets the session cookie itself (login, logout) wins; ``/events`` is never
    re-signed because ``require_session`` does not store its session.
    """

    def __init__(self, app: ASGIApp, codec: SessionCodec, clock: Callable[[], float], secure: bool) -> None:
        self.app = app
        self.codec = codec
        self.clock = clock
        self.secure = secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        path: str = scope["path"]

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                content_type = headers.get("content-type", "")
                if content_type.startswith(("text/html", "text/event-stream")):
                    for name, value in SECURITY_HEADERS.items():
                        headers[name] = value
                elif path.startswith("/static/") and message.get("status") == 200:
                    headers["Cache-Control"] = STATIC_CACHE
                session = state.get("session")
                sets_cookie = any(v.startswith(f"{COOKIE_NAME}=") for v in headers.getlist("set-cookie"))
                if isinstance(session, Session) and not sets_cookie:
                    headers.append("set-cookie", self._refreshed(session))
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _refreshed(self, session: Session) -> str:
        now = self.clock()
        touched = self.codec.touch(session, now)
        max_age = max(int(self.codec.ends_at(touched) - now), 0)
        return session_cookie(self.codec.encode(touched), max_age, self.secure)

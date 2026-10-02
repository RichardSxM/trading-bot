"""Everything a request handler needs, built once by ``create_app`` and kept on ``app.state``."""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request
from fastapi.templating import Jinja2Templates

from dashboard.auth.lockout import Lockout
from dashboard.auth.password import PasswordChecker
from dashboard.auth.session import SessionCodec
from dashboard.auth.totp import TotpGuard
from dashboard.live.poller import Poller
from dashboard.settings import DashboardSettings


@dataclass(frozen=True)
class AppContext:
    """The dashboard's long lived objects; ``clock`` returns UTC seconds since the epoch."""

    settings: DashboardSettings
    templates: Jinja2Templates
    codec: SessionCodec
    passwords: PasswordChecker
    totp: TotpGuard
    lockout: Lockout
    poller: Poller
    clock: Callable[[], float]


def app_context(request: Request) -> AppContext:
    """The ``AppContext`` of the app serving ``request``."""
    context: AppContext = request.app.state.context
    return context

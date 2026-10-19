"""Everything a request handler needs, built once by ``create_app`` and kept on ``app.state``."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from fastapi import Request
from fastapi.templating import Jinja2Templates
from redis.typing import EncodableT, FieldT

from dashboard.auth.lockout import Lockout
from dashboard.auth.password import PasswordChecker
from dashboard.auth.session import SessionCodec
from dashboard.auth.totp import TotpGuard
from dashboard.data.strategies import StrategiesView
from dashboard.data.trades import TradesView
from dashboard.live.poller import Poller
from dashboard.live.state import Entry
from dashboard.settings import DashboardSettings
from trading_core.recording.gate_queries import PromotionLine, StudyDetail


class LiveRedis(Protocol):
    """The Redis calls the dashboard makes (redis.asyncio.Redis satisfies it)."""

    def get(self, name: str) -> Awaitable[bytes | str | None]: ...

    def xadd(
        self,
        name: str,
        fields: dict[FieldT, EncodableT],
        id: str = ...,
        maxlen: int | None = ...,
        approximate: bool = ...,
    ) -> Awaitable[bytes | str]: ...

    def xrevrange(
        self, name: str, max: str = ..., min: str = ..., count: int | None = ...
    ) -> Awaitable[Sequence[Entry] | None]: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class AppContext:
    """The dashboard's long lived objects; ``clock`` returns UTC seconds since the epoch.

    ``read_trades(page, fills_page)`` reads the two Trades lists from Postgres in a thread, and
    ``read_strategies()`` the strategy ranking and paper progress, ``read_study(id)`` one study, and
    ``read_promotions(page)`` the promotion log (spec 0015 AC-18 to AC-21).
    ``trusted_peers`` are the socket peers besides loopback whose client headers key the lockout.
    """

    settings: DashboardSettings
    templates: Jinja2Templates
    codec: SessionCodec
    passwords: PasswordChecker
    totp: TotpGuard
    lockout: Lockout
    poller: Poller
    redis: LiveRedis
    clock: Callable[[], float]
    read_trades: Callable[[int, int], Awaitable[TradesView]]
    read_strategies: Callable[[], Awaitable[StrategiesView]]
    read_study: Callable[[UUID], Awaitable[StudyDetail | None]]
    read_promotions: Callable[[int], Awaitable[Sequence[PromotionLine]]]
    trusted_peers: frozenset[str] = frozenset()


def app_context(request: Request) -> AppContext:
    """The ``AppContext`` of the app serving ``request``."""
    context: AppContext = request.app.state.context
    return context

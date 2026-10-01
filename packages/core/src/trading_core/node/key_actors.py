"""Live Kraken key age actor for the trader (spec 0003 AC-9).

``KeyAgeActor`` warns at start and once per UTC day once the key is past its rotation age.
The rule is a pure function in ``trading_core.secrets``; the actor only wires it to the
Nautilus clock. (The startup key check is not an actor: see ``node.key_check.watch_key_check``.)
"""

import logging
from datetime import UTC, date, datetime, time, timedelta

from nautilus_trader.common.actor import Actor
from nautilus_trader.common.config import ActorConfig

from trading_core.secrets import KEY_MAX_AGE_DAYS, key_rotation_age

log = logging.getLogger(__name__)

_AGE_TIMER = "kraken_key_age"


class KeyAgeConfig(ActorConfig, frozen=True):
    rotated_on: date
    max_age_days: int = KEY_MAX_AGE_DAYS


class KeyAgeActor(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Logs ``kraken_key_rotation_due`` at start and every UTC midnight while the key is too old."""

    def __init__(self, config: KeyAgeConfig) -> None:
        super().__init__(config)
        self._rotated_on = config.rotated_on
        self._max_age_days = config.max_age_days

    def on_start(self) -> None:
        self._check()
        # A Nautilus timer first fires at start_time + interval, so starting at today's
        # midnight makes it fire at every following UTC midnight.
        today_midnight = datetime.combine(self.clock.utc_now().date(), time(), tzinfo=UTC)
        self.clock.set_timer(
            _AGE_TIMER, timedelta(days=1), start_time=today_midnight, callback=lambda _event: self._check()
        )

    def on_stop(self) -> None:
        if _AGE_TIMER in self.clock.timer_names:
            self.clock.cancel_timer(_AGE_TIMER)

    def _check(self) -> None:
        age_days = key_rotation_age(self._rotated_on, self.clock.utc_now().date())
        if age_days > self._max_age_days:
            log.warning(
                "kraken key is due for rotation",
                extra={
                    "fields": {
                        "code": "kraken_key_rotation_due",
                        "rotated_on": self._rotated_on.isoformat(),
                        "age_days": age_days,
                    }
                },
            )

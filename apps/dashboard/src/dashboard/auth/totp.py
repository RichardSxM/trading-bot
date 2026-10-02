"""Authenticator codes with a replay guard (spec 0004 AC-3).

A code is accepted for the current 30 s step and one step either side. Once a step is
accepted (with the right password), that step and every older one are refused, so a code
seen over a shoulder cannot be used again. The guard lives in process memory.
"""

import asyncio
import hmac
from typing import Final

import pyotp

STEP_S: Final = 30
WINDOW: Final = (-1, 0, 1)


class TotpGuard:
    """Matches codes to their time step and remembers the last accepted step."""

    def __init__(self, seed: str) -> None:
        self._totp = pyotp.TOTP(seed, interval=STEP_S)
        self._last_accepted: int | None = None
        self._lock = asyncio.Lock()

    def matched_step(self, code: str, now: float) -> int | None:
        """The step (``floor(now / 30) + offset``) whose code equals ``code``, else None.

        Compares every offset in constant time, without stopping at the first match.
        """
        current = int(now // STEP_S)
        matched: int | None = None
        for offset in WINDOW:
            step = current + offset
            if hmac.compare_digest(self._totp.at(step * STEP_S).encode(), code.encode()):
                matched = step
        return matched

    def is_replay(self, step: int) -> bool:
        """True when ``step`` is not newer than the last accepted step."""
        return self._last_accepted is not None and step <= self._last_accepted

    @property
    def lock(self) -> asyncio.Lock:
        """Held around the replay check and ``accept``, so two logins cannot both use one step."""
        return self._lock

    def accept(self, step: int) -> None:
        """Record ``step`` as the last accepted one; call only when the password matched too."""
        self._last_accepted = step

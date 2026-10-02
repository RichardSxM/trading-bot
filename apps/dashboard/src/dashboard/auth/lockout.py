"""Login lockout (spec 0004 AC-2), in process memory; a restart clears it.

Per key: 5 failures within a fixed 15 minute window (from that key's first failure) lock
the key until the window ends. Across all keys: 20 failures in 15 minutes lock every new
login the same way. A locked attempt does not count or extend anything; a success resets
the key. At most 1,000 keys are held, the oldest evicted first.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Final

WINDOW_S: Final = 15 * 60
MAX_KEY_FAILURES: Final = 5
MAX_GLOBAL_FAILURES: Final = 20
MAX_KEYS: Final = 1000


@dataclass
class _Window:
    started: float
    failures: int


class Lockout:
    """Counts failed logins per key and in total."""

    def __init__(
        self,
        window_s: float = WINDOW_S,
        max_key_failures: int = MAX_KEY_FAILURES,
        max_global_failures: int = MAX_GLOBAL_FAILURES,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._window_s = window_s
        self._max_key = max_key_failures
        self._max_global = max_global_failures
        self._max_keys = max_keys
        self._keys: OrderedDict[str, _Window] = OrderedDict()
        self._global: deque[float] = deque()

    def is_locked(self, key: str, now: float) -> bool:
        """True when ``key``, or every login, is locked at ``now``."""
        self._expire_global(now)
        if len(self._global) >= self._max_global:
            return True
        window = self._current(key, now)
        return window is not None and window.failures >= self._max_key

    def record_failure(self, key: str, now: float) -> None:
        """Count one failed attempt for ``key`` (never call it for a locked attempt)."""
        self._expire_global(now)
        self._global.append(now)
        window = self._current(key, now)
        if window is None:
            self._keys[key] = _Window(started=now, failures=1)
            while len(self._keys) > self._max_keys:
                self._keys.popitem(last=False)
        else:
            window.failures += 1

    def record_success(self, key: str) -> None:
        """Reset ``key``'s window after a successful login."""
        self._keys.pop(key, None)

    def _current(self, key: str, now: float) -> _Window | None:
        window = self._keys.get(key)
        if window is not None and now - window.started >= self._window_s:
            del self._keys[key]
            return None
        return window

    def _expire_global(self, now: float) -> None:
        while self._global and now - self._global[0] >= self._window_s:
            self._global.popleft()

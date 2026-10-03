"""The password check against the argon2id hash from ``dashboard_password_hash``."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordChecker:
    """Verifies a submitted password. Slow on purpose (about 64 MiB and t=3): run it in a thread."""

    def __init__(self, password_hash: str, hasher: PasswordHasher | None = None) -> None:
        self._hash = password_hash
        self._hasher = hasher or PasswordHasher()

    def verify(self, password: str) -> bool:
        """True when ``password`` matches the hash; never raises for a wrong one."""
        try:
            return self._hasher.verify(self._hash, password)
        except VerificationError, InvalidHashError:
            return False

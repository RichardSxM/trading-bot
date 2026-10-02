"""The stateless session cookie ``kfs_session`` (spec 0004 AC-4).

A signed (not encrypted) JSON payload: ``iat`` (login time), ``seen`` (last request),
``csrf`` (the per session CSRF token), and ``pw`` (the password hash fingerprint, so a new
password ends old sessions). Rotating ``dashboard_session_key`` ends every session.
"""

import secrets
from dataclasses import dataclass, replace
from typing import Final

from itsdangerous import BadSignature, URLSafeSerializer

COOKIE_NAME: Final = "kfs_session"
SALT: Final = "kfs-session-v1"


@dataclass(frozen=True)
class Session:
    """A valid session's fields; times are UTC seconds since the epoch."""

    iat: int
    seen: int
    csrf: str
    pw: str


class SessionCodec:
    """Signs and checks session cookies, applying the idle and hard cap limits."""

    def __init__(self, key: str, password_fingerprint: str, idle_s: float, max_s: float) -> None:
        self._serializer = URLSafeSerializer(key, salt=SALT)
        self._pw = password_fingerprint
        self._idle_s = idle_s
        self._max_s = max_s

    def new(self, now: float) -> Session:
        """A fresh session at login."""
        t = int(now)
        return Session(iat=t, seen=t, csrf=secrets.token_urlsafe(32), pw=self._pw)

    def touch(self, session: Session, now: float) -> Session:
        """The same session with ``seen`` moved to ``now`` (the sliding idle window)."""
        return replace(session, seen=int(now))

    def encode(self, session: Session) -> str:
        """The signed cookie value."""
        return self._serializer.dumps(
            {"iat": session.iat, "seen": session.seen, "csrf": session.csrf, "pw": session.pw}
        )

    def decode(self, cookie: str | None, now: float) -> Session | None:
        """The session in ``cookie``, or None when it is missing, tampered, expired, or from an old password."""
        if not cookie:
            return None
        try:
            payload = self._serializer.loads(cookie)
        except BadSignature:
            return None
        session = _parse(payload)
        if session is None or session.pw != self._pw:
            return None
        if now >= self.ends_at(session):
            return None
        return session

    def ends_at(self, session: Session) -> float:
        """When the session expires: idle limit after ``seen`` or the hard cap after ``iat``, whichever is sooner."""
        return min(session.seen + self._idle_s, session.iat + self._max_s)


def _parse(payload: object) -> Session | None:
    if not isinstance(payload, dict):
        return None
    iat, seen, csrf, pw = (payload.get(name) for name in ("iat", "seen", "csrf", "pw"))
    # bool is an int subclass; a true or false time is a malformed payload.
    if not isinstance(iat, int) or isinstance(iat, bool) or not isinstance(seen, int) or isinstance(seen, bool):
        return None
    if not isinstance(csrf, str) or not isinstance(pw, str):
        return None
    return Session(iat=iat, seen=seen, csrf=csrf, pw=pw)

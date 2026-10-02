"""Where to send the browser after login (spec 0004 AC-1); never another origin."""

import re
from typing import Final
from urllib.parse import urlsplit

_SAFE: Final = re.compile(r"^/[A-Za-z0-9/_-]*$")


def safe_next(candidate: str | None) -> str:
    """``candidate`` when it is a plain local path other than ``/login``, else ``/``.

    A leading ``//`` is refused too: browsers read it as another host (``//evil`` on the tailnet).
    """
    if candidate and _SAFE.fullmatch(candidate) and candidate != "/login" and not candidate.startswith("//"):
        return candidate
    return "/"


def referer_path(referer: str | None) -> str:
    """The path of a ``Referer`` header, validated like any ``next``; ``/`` when absent."""
    if not referer:
        return "/"
    try:
        return safe_next(urlsplit(referer).path)
    except ValueError:
        return "/"

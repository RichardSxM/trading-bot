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


# Every page inside the shell; ``web/page_routes.PAGES`` must list the same paths.
PAGE_PATHS: Final = frozenset({"/", "/trades", "/strategies", "/system", "/styleguide"})


def referer_page(referer: str | None) -> str:
    """The page a ``Referer`` names, ``/`` for anything else.

    A form posted from the stop error page has ``Referer: /control/stop``; sending the browser
    there would be a ``GET`` the route refuses with 405.
    """
    path = referer_path(referer)
    return path if path in PAGE_PATHS else "/"

"""Names that keep demo and live state apart in a shared Redis.

NautilusTrader cache keys have a fixed format (``trader-<TRADER_ID>:...``), so the
environment lives in the node id tag (``TRADER-DEMO``). Our own Redis keys carry a
lowercase ``<env>:<process>:`` prefix. Both are derived here from ``TRADING_ENV``.
"""

from trading_core.settings import TradingEnv


class NodeIdMismatch(ValueError):
    """An explicitly configured node id whose tag disagrees with TRADING_ENV."""


def env_tag(env: TradingEnv) -> str:
    """Uppercase tag for Nautilus node ids, e.g. ``DEMO``."""
    return env.upper()


def key_prefix(env: TradingEnv, scope: str) -> str:
    """Prefix for keys we write ourselves, e.g. ``demo:trader:`` or ``demo:snapshot:``."""
    return f"{env}:{scope}:"


def node_id(name: str, env: TradingEnv, explicit: str | None = None) -> str:
    """Nautilus node id ``NAME-TAG`` for this environment.

    An explicit id is accepted only when its tag (the part after the first ``-``,
    compared case insensitively) equals the environment.
    """
    if explicit is None:
        return f"{name.upper()}-{env_tag(env)}"
    _, sep, tag = explicit.partition("-")
    if not sep or tag.lower() != env:
        raise NodeIdMismatch(
            f"node id {explicit!r} does not match TRADING_ENV={env!r}; expected a tag of {env_tag(env)!r}"
        )
    return explicit

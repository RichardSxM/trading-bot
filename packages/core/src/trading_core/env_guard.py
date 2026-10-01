"""Demo can never trade for real: the guard every node builder runs before ``build()``.

It checks the final node config against an allowlist of execution clients, and refuses
when Kraken keys are in the environment where none may be (the Kraken adapter reads
them from the environment on its own when its key fields are empty).
"""

import os
from collections.abc import Mapping
from typing import Literal

from trading_core.settings import TradingEnv

NodeProcess = Literal["trader", "recorder"]

# Execution clients each node may build, by environment. Empty for the scaffold.
# Feature 6 adds the sandbox to ("trader", "demo"); feature 4 and the live canary add
# Kraken to ("trader", "live"). The recorder never gets one, in any environment.
ALLOWED_EXEC_CLIENTS: dict[tuple[NodeProcess, TradingEnv], frozenset[str]] = {
    ("trader", "demo"): frozenset(),
    ("trader", "live"): frozenset(),
    ("recorder", "demo"): frozenset(),
    ("recorder", "live"): frozenset(),
}

KRAKEN_KEY_VARS = (
    "KRAKEN_FUTURES_API_KEY",
    "KRAKEN_FUTURES_API_SECRET",
    "KRAKEN_FUTURES_DEMO_API_KEY",
    "KRAKEN_FUTURES_DEMO_API_SECRET",
)


class EnvGuardRefused(RuntimeError):
    """The node must not start. The message names the client or variable, never a value."""


def check(
    exec_clients: Mapping[str, object],
    process: NodeProcess,
    env: TradingEnv,
    environ: Mapping[str, str] = os.environ,
) -> None:
    """Raise EnvGuardRefused unless this node's execution clients and environment are allowed.

    Pass ``config.exec_clients`` from the final ``TradingNodeConfig``.
    """
    allowed = ALLOWED_EXEC_CLIENTS[(process, env)]
    extra = sorted(name for name in exec_clients if name not in allowed)
    if extra:
        raise EnvGuardRefused(
            f"{process} in {env} may not build execution client(s) {extra}; "
            f"allowed: {sorted(allowed) or 'none'}"
        )
    if env == "demo" or process == "recorder":
        present = [name for name in KRAKEN_KEY_VARS if environ.get(name, "").strip()]
        if present:
            raise EnvGuardRefused(f"{process} in {env} must not see Kraken keys, but {present} are set")

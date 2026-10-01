"""Demo can never trade for real, and Kraken keys only ever come from files (spec 0001, 0003).

Two checks, run by ``boot``. ``check_environ`` runs first and needs no secrets: it refuses
any Kraken key variable in every process and environment, because the Kraken adapter reads
them from the environment on its own when its key fields are empty. ``check_exec_clients``
runs once the secrets are loaded: it checks the final node config against an allowlist of
execution clients.
"""

import os
from collections.abc import Mapping
from typing import Literal

from trading_core.settings import TradingEnv

NodeProcess = Literal["trader", "recorder"]

ExecClientAllowlist = Mapping[tuple[NodeProcess, TradingEnv], frozenset[str]]

# Execution clients each node may build, by environment. Feature 6 adds the sandbox to
# ("trader", "demo"); feature 19 (go live) adds Kraken to ("trader", "live"). The recorder
# never gets one, in any environment.
ALLOWED_EXEC_CLIENTS: ExecClientAllowlist = {
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
    """The process must not start. The message names the client or variable, never a value."""


def check_environ(process: str, environ: Mapping[str, str] = os.environ) -> None:
    """Raise EnvGuardRefused when any Kraken key variable is set and not empty, in any process or environment."""
    present = [name for name in KRAKEN_KEY_VARS if environ.get(name, "").strip()]
    if present:
        raise EnvGuardRefused(f"{process} must not see Kraken keys in the environment, but {present} are set")


def check_exec_clients(
    exec_clients: Mapping[str, object],
    process: NodeProcess,
    env: TradingEnv,
    allowlist: ExecClientAllowlist = ALLOWED_EXEC_CLIENTS,
) -> None:
    """Raise EnvGuardRefused unless every execution client is allowed for this node and environment.

    Pass ``config.exec_clients`` from the final ``TradingNodeConfig``.
    """
    allowed = allowlist[(process, env)]
    extra = sorted(name for name in exec_clients if name not in allowed)
    if extra:
        raise EnvGuardRefused(
            f"{process} in {env} may not build execution client(s) {extra}; allowed: {sorted(allowed) or 'none'}"
        )

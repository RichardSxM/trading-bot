"""Canary harness for the leak test (spec 0003 AC-6, AC-8). Not a test file: test_canary.py runs it.

It is the live trader startup with three changes, so it needs no network and touches nothing
real: an in memory cache instead of Redis, no data client, and every Kraken URL pointed at a
closed local port. It loads the secrets through the real loader, builds the real live config,
checks it against an allowlist that permits Kraken, and runs the node with the real key check
(2 s, no backoff), which must fail closed: exit 1 with ``kraken_auth_failed``. It skips boot's
clock check, which needs NTP.
"""

import sys

import msgspec
from nautilus_trader.config import CacheConfig

from trader.main import TraderSettings, build_config, run_node, trader_secrets
from trading_core import env_guard
from trading_core.env_guard import ALLOWED_EXEC_CLIENTS
from trading_core.logs import configure_logging
from trading_core.secrets import load_required

CLOSED_PORT_URLS = ("http://127.0.0.1:9", "ws://127.0.0.1:9")
CANARY_ALLOWLIST = dict(ALLOWED_EXEC_CLIENTS) | {("trader", "live"): frozenset({"KRAKEN"})}


def main() -> int:
    settings = TraderSettings()
    configure_logging(process="trader", env=settings.trading_env, level=settings.log_level)
    env_guard.check_environ("trader")
    secrets = load_required(trader_secrets(settings), settings.secrets_dir)
    config = build_config(settings, secrets, kraken_urls=CLOSED_PORT_URLS)
    config = msgspec.structs.replace(config, cache=CacheConfig(), data_clients={})
    env_guard.check_exec_clients(config.exec_clients, "trader", settings.trading_env, allowlist=CANARY_ALLOWLIST)
    return run_node(config, settings, key_check_timeout_s=2.0, fail_backoff_s=0.0)


if __name__ == "__main__":
    sys.exit(main())

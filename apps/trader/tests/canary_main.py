"""Canary harness for the leak test (spec 0003 AC-6, AC-8). Not a test file: test_canary.py runs it.

It is the live trader startup with three changes, so it needs no network and touches nothing
real: an in memory cache instead of Redis, no data client, and every Kraken URL pointed at a
closed local port. It loads the secrets through the real loader, builds the real live config,
checks it against an allowlist that permits Kraken, and runs the node with the real key check
(2 s, no backoff), which must fail closed: exit 1 with ``kraken_auth_failed``. It skips boot's
clock check, which needs NTP.

Before the node starts it also logs every loaded value on purpose, in a message, a field,
exception text, and a database URL (AC-5), so the test fails if our redaction ever breaks:
nothing in the real startup routes a secret through our formatter.
"""

import logging
import sys

import msgspec
from nautilus_trader.config import CacheConfig
from pydantic import SecretStr

from trader.main import TraderSettings, build_config, kraken_exchange_view, run_node, trader_secrets
from trading_core import env_guard
from trading_core.env_guard import ALLOWED_EXEC_CLIENTS
from trading_core.logs import configure_logging
from trading_core.secrets import POSTGRES_PASSWORD, load_required

CLOSED_PORT_URLS = ("http://127.0.0.1:9", "ws://127.0.0.1:9")
CANARY_ALLOWLIST = dict(ALLOWED_EXEC_CLIENTS) | {("trader", "live"): frozenset({"KRAKEN"})}

log = logging.getLogger("canary")


def leak_on_purpose(settings: TraderSettings, secrets: dict[str, SecretStr]) -> None:
    for name, secret in secrets.items():
        value = secret.get_secret_value()
        log.warning(f"canary probe {name} {value}", extra={"fields": {"probe": value}})
        try:
            raise RuntimeError(f"canary probe {value}")
        except RuntimeError:
            log.exception("canary probe exception")
    log.warning("canary probe url", extra={"fields": {"url": settings.database_url(secrets[POSTGRES_PASSWORD])}})


def main() -> int:
    settings = TraderSettings()
    configure_logging(process="trader", env=settings.trading_env, level=settings.log_level)
    env_guard.check_environ("trader")
    secrets = load_required(trader_secrets(settings), settings.secrets_dir)
    leak_on_purpose(settings, secrets)
    config = build_config(settings, secrets, kraken_urls=CLOSED_PORT_URLS)
    config = msgspec.structs.replace(config, cache=CacheConfig(), data_clients={})
    env_guard.check_exec_clients(config.exec_clients, "trader", settings.trading_env, allowlist=CANARY_ALLOWLIST)
    # The reconciliation view gets the closed port too, so nothing reaches Kraken.
    exchange = kraken_exchange_view(settings, secrets, base_url=CLOSED_PORT_URLS[0])
    return run_node(config, settings, 2.0, secrets, fail_backoff_s=0.0, exchange=exchange)


if __name__ == "__main__":
    sys.exit(main())

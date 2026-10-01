"""The first thing every process runs: settings, logging, env guard, secrets, config guard, clock check."""

import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn

from pydantic import SecretStr, ValidationError

from trading_core import clock, env_guard
from trading_core.env_guard import EnvGuardRefused
from trading_core.logs import configure_logging
from trading_core.secrets import SecretUnavailable, load_required
from trading_core.settings import Settings

log = logging.getLogger(__name__)

Secrets = Mapping[str, SecretStr]


@dataclass(frozen=True)
class Boot[S: Settings]:
    """What a process gets back from ``boot``: its validated settings and its loaded secrets."""

    settings: S
    secrets: Secrets


def boot[S: Settings = Settings](
    kind: clock.ProcessKind,
    # mypy cannot tie a parameter default to a type parameter (python/mypy#3737).
    settings_cls: type[S] = Settings,  # type: ignore[assignment]
    guard: Callable[[S, Secrets], None] | None = None,
    # The secret names, or a function of the validated settings when they depend on them
    # (the trader needs its Kraken files only in live).
    secrets: tuple[str, ...] | Callable[[S], tuple[str, ...]] = (),
) -> Boot[S]:
    """Start a process in this order, exiting with status 1 at the first refusal.

    1. Settings from the environment (invalid config refuses).
    2. JSON logging.
    3. ``env_guard.check_environ``: no Kraken key variable in any process (needs no secrets).
    4. The named ``secrets`` from their files (spec 0003); an unusable one refuses.
    5. ``guard``, the node's config guard, with the settings and secrets (see trading_core.env_guard).
    6. The startup clock check.
    """
    process = os.environ.get("PROCESS_NAME", "unknown")
    try:
        settings = settings_cls()  # fields come from the environment
    except ValidationError as exc:
        # Logging is not configured yet (it needs the env), so log with what we have.
        configure_logging(process=process, env=os.environ.get("TRADING_ENV", ""), level="INFO")
        log.critical(
            "invalid configuration, refusing to start",
            extra={"fields": {"errors": exc.errors(include_url=False, include_input=False)}},
        )
        sys.exit(1)

    configure_logging(process=settings.process_name, env=settings.trading_env, level=settings.log_level)
    log.info("starting", extra={"fields": {"kind": kind}})

    try:
        env_guard.check_environ(settings.process_name)
    except EnvGuardRefused as exc:
        _refuse_guard(exc)

    try:
        names = secrets(settings) if callable(secrets) else secrets
        loaded = load_required(names, settings.secrets_dir)
    except SecretUnavailable as exc:
        log.critical(
            "secret unavailable, refusing to start",
            extra={"fields": {"code": "secret_unavailable", "secret": exc.name, "reason": exc.reason}},
        )
        sys.exit(1)

    if guard is not None:
        try:
            guard(settings, loaded)
        except EnvGuardRefused as exc:
            _refuse_guard(exc)

    result = clock.measure(settings.clock_server_list)
    try:
        clock.enforce(result, kind=kind, env=settings.trading_env, max_offset_ms=settings.max_clock_offset_ms)
    except clock.ClockCheckFailed as exc:
        log.critical(str(exc), extra={"fields": {"offset_ms": result.offset_ms, "answered": result.answered}})
        sys.exit(1)
    return Boot(settings=settings, secrets=loaded)


def _refuse_guard(exc: EnvGuardRefused) -> NoReturn:
    log.error(
        "env guard refused, refusing to start",
        extra={"fields": {"code": "env_guard_refused", "reason": str(exc)}},
    )
    sys.exit(1)

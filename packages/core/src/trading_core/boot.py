"""The first thing every process runs: settings, logging, env guard, clock check."""

import logging
import os
import sys
from collections.abc import Callable

from pydantic import ValidationError

from trading_core import clock
from trading_core.env_guard import EnvGuardRefused
from trading_core.logs import configure_logging
from trading_core.settings import Settings

log = logging.getLogger(__name__)


def boot[S: Settings = Settings](
    kind: clock.ProcessKind,
    # mypy cannot tie a parameter default to a type parameter (python/mypy#3737).
    settings_cls: type[S] = Settings,  # type: ignore[assignment]
    guard: Callable[[S], None] | None = None,
) -> S:
    """Load settings, set up JSON logs, run the env guard, then the startup clock check.

    ``guard`` is the node's env guard (see trading_core.env_guard); it runs right after
    settings validate. Exits the process (status 1) when config is invalid, the guard
    refuses, or the clock check refuses.
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

    if guard is not None:
        try:
            guard(settings)
        except EnvGuardRefused as exc:
            log.error(
                "env guard refused, refusing to start",
                extra={"fields": {"code": "env_guard_refused", "reason": str(exc)}},
            )
            sys.exit(1)

    result = clock.measure(settings.clock_server_list)
    try:
        clock.enforce(result, kind=kind, env=settings.trading_env, max_offset_ms=settings.max_clock_offset_ms)
    except clock.ClockCheckFailed as exc:
        log.critical(str(exc), extra={"fields": {"offset_ms": result.offset_ms, "answered": result.answered}})
        sys.exit(1)
    return settings

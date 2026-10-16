"""Container states into condition checks (spec 0013 AC-4): ``service_down`` and ``container_unhealthy``.

Read every 15 s from the Docker listing. The trader, recorder, postgres, and redis must always
run. ``clock`` runs only under the laptop profile, so it is expected only while a clock
container exists in the project (stopped or not); on the VPS there is none. A service that
leaves the listing (its container removed) passes its health check, so nothing stays open.
"""

from collections.abc import Iterable
from datetime import datetime
from typing import Final

from alerter.sources.docker_api import Container
from trading_core.alerts.model import CheckResult
from trading_core.alerts.rules import CONTAINER_UNHEALTHY, CORE_SERVICES, SERVICE_DOWN

# Present only under a Compose profile, so absence alone is not "down".
PROFILE_ONLY: Final = frozenset({"clock"})
SKIPPED: Final = frozenset({"migrate", "tools", "lab", "dashboard-credentials"})


def container_checks(containers: Iterable[Container], now: datetime, known: Iterable[str] = ()) -> list[CheckResult]:
    """One ``service_down`` result per core service and one ``container_unhealthy`` per service.

    ``known`` names services seen in earlier listings; one missing now passes its health check.
    """
    listed = list(containers)
    by_service: dict[str, list[Container]] = {}
    for container in listed:
        by_service.setdefault(container.service, []).append(container)
    results = []
    for service in CORE_SERVICES:
        present = by_service.get(service, [])
        running = any(c.state == "running" for c in present) or (service in PROFILE_ONLY and not present)
        state = ", ".join(sorted({c.state for c in present})) or "none"
        results.append(CheckResult(SERVICE_DOWN.id, ok=running, at=now, subject=service, fields={"state": state}))
    for service in sorted(set(by_service) | set(known)):
        if service in SKIPPED:
            continue
        sick = [c for c in by_service.get(service, []) if "(unhealthy)" in c.status]
        status = sick[0].status if sick else ""
        results.append(
            CheckResult(CONTAINER_UNHEALTHY.id, ok=not sick, at=now, subject=service, fields={"status": status})
        )
    return results

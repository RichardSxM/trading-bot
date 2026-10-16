"""The alerter's view of its Compose project through the socket proxy (spec 0013 Build plan 2.1).

The project is the one the alerter's own container belongs to (found by inspecting its hostname,
which Docker sets to the short container id), so other containers on the host are never read.
"""

import logging
from datetime import datetime

from alerter.sources.docker_api import Container, DockerApi
from alerter.sources.docker_logs import SKIPPED, LogReader
from trading_core.alerts.model import Observation

log = logging.getLogger(__name__)


class DockerWatch:
    """Lists the project's containers, reads their new lines, and reports their start times."""

    def __init__(self, api: DockerApi, reader: LogReader, own_container: str) -> None:
        self._api = api
        self._reader = reader
        self._own = own_container
        self._project: str | None = None

    @property
    def ok_at(self) -> datetime | None:
        """When a full log pass last succeeded (AC-2)."""
        return self._reader.ok_at

    async def containers(self) -> list[Container]:
        """The project's containers; the first call finds the project. Raises ``DockerApiError``."""
        if self._project is None:
            self._project = await self._api.project_of(self._own)
            log.info(
                "docker project found", extra={"fields": {"code": "alerter_docker_project", "project": self._project}}
            )
        return await self._api.containers(self._project)

    async def lines(self) -> list[Observation]:
        """Every new line of the project's containers. Raises ``DockerApiError`` or ``RedisError``."""
        return await self._reader.poll(await self.containers())

    async def states(self) -> tuple[list[Container], dict[str, datetime]]:
        """The containers, and the start time of each running one by service. Raises ``DockerApiError``."""
        containers = await self.containers()
        starts: dict[str, datetime] = {}
        for container in containers:
            if container.state != "running" or container.service in SKIPPED:
                continue
            started = await self._api.started_at(container.id)
            if started is not None:
                starts[container.service] = max(started, starts.get(container.service, started))
        return containers, starts

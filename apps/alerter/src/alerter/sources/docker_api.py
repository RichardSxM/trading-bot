"""The Docker Engine API through the read only socket proxy (spec 0013, *Security model*).

Only GET calls under ``/containers``: the listing, one container's inspect, and its logs. The
proxy allows nothing else.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx

TIMEOUT_S: Final = 10.0
PROJECT_LABEL: Final = "com.docker.compose.project"
SERVICE_LABEL: Final = "com.docker.compose.service"
ONEOFF_LABEL: Final = "com.docker.compose.oneoff"


class DockerApiError(Exception):
    """A Docker API call failed: the proxy is unreachable, or it answered with an error."""


@dataclass(frozen=True)
class Container:
    """One container of the Compose project, as the listing shows it."""

    id: str
    service: str
    state: str
    status: str


class DockerApi:
    """The three calls the alerter needs."""

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def _get(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        try:
            response = await self._http.get(f"{self._base}{path}", params=params, timeout=TIMEOUT_S)
        except httpx.HTTPError as exc:
            raise DockerApiError(f"GET {path}: {type(exc).__name__}") from exc
        if not response.is_success:
            raise DockerApiError(f"GET {path}: HTTP {response.status_code}")
        return response

    async def inspect(self, container_id: str) -> Mapping[str, object]:
        """``GET /containers/<id>/json``."""
        body = (await self._get(f"/containers/{container_id}/json", {})).json()
        if not isinstance(body, dict):
            raise DockerApiError("inspect: not an object")
        return body

    async def project_of(self, container_id: str) -> str:
        """The Compose project of a container (the alerter's own, found by its hostname)."""
        labels = _labels(await self.inspect(container_id), "Config")
        project = labels.get(PROJECT_LABEL)
        if not project:
            raise DockerApiError(f"container {container_id} has no {PROJECT_LABEL} label")
        return project

    async def started_at(self, container_id: str) -> datetime | None:
        """``State.StartedAt`` of a container; None when Docker gives no usable time."""
        state = (await self.inspect(container_id)).get("State")
        raw = state.get("StartedAt") if isinstance(state, dict) else None
        if not isinstance(raw, str) or raw.startswith("0001-"):
            return None
        return _rfc3339(raw)

    async def containers(self, project: str) -> list[Container]:
        """Every container of ``project``, running or not, without one shot ``compose run`` containers."""
        filters = json.dumps({"label": [f"{PROJECT_LABEL}={project}"]})
        body = (await self._get("/containers/json", {"all": "1", "filters": filters})).json()
        if not isinstance(body, list):
            raise DockerApiError("containers: not a list")
        found = []
        for item in body:
            if not isinstance(item, dict):
                continue
            labels = _labels(item, None)
            if labels.get(ONEOFF_LABEL, "False") == "True" or SERVICE_LABEL not in labels:
                continue
            found.append(
                Container(
                    id=str(item.get("Id", "")),
                    service=labels[SERVICE_LABEL],
                    state=str(item.get("State", "")),
                    status=str(item.get("Status", "")),
                )
            )
        return found

    async def logs(self, container_id: str, since: str) -> bytes:
        """Stdout and stderr since ``since`` (Unix seconds with a fraction), each line timestamped."""
        params = {"stdout": "1", "stderr": "1", "timestamps": "1", "since": since}
        return (await self._get(f"/containers/{container_id}/logs", params)).content


def _labels(body: Mapping[str, object], section: str | None) -> dict[str, str]:
    holder = body.get(section) if section else body
    labels = holder.get("Labels") if isinstance(holder, dict) else None
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def _rfc3339(raw: str) -> datetime:
    return ns_to_datetime(rfc3339_ns(raw))


def rfc3339_ns(raw: str) -> int:
    """Nanoseconds since the epoch of a Docker RFC 3339 time (``2026-10-16T03:00:00.12345Z``).

    Docker trims trailing zeros from the fraction, so the text alone does not sort; the number does.
    Raises ``ValueError`` on anything else.
    """
    if not raw.endswith("Z"):
        raise ValueError(f"not a UTC RFC 3339 time: {raw!r}")
    whole, _, fraction = raw[:-1].partition(".")
    if fraction and not fraction.isdigit():
        raise ValueError(f"bad fraction in {raw!r}")
    seconds = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return int(seconds.timestamp()) * 1_000_000_000 + int((fraction or "0").ljust(9, "0")[:9])


def ns_to_rfc3339(ns: int) -> str:
    """Nanoseconds as RFC 3339 with all nine fraction digits, the cursor format."""
    seconds, fraction = divmod(ns, 1_000_000_000)
    return f"{datetime.fromtimestamp(seconds, UTC).strftime('%Y-%m-%dT%H:%M:%S')}.{fraction:09d}Z"


def ns_to_since(ns: int) -> str:
    """Nanoseconds as the Unix seconds and fraction the logs ``since`` parameter takes."""
    seconds, fraction = divmod(ns, 1_000_000_000)
    return f"{seconds}.{fraction:09d}"


def ns_to_datetime(ns: int) -> datetime:
    """Nanoseconds as an aware UTC datetime, cut to microseconds."""
    seconds, fraction = divmod(ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=fraction // 1000)

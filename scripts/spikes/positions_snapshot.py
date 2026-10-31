"""Spec 0025 task 11: print ``{env}:snapshot:positions`` as the dashboard reads it (read only).

Runs in the dashboard image on the VPS, with the dashboard's own ACL user and secret file:
  kfs-compose run --rm --no-deps -T -v <dir>:/spike:ro --entrypoint python dashboard -I /spike/positions_snapshot.py
"""

import asyncio
import json

from dashboard.settings import DashboardSettings
from dashboard.web.app import redis_client
from trading_core.secrets import REDIS_PASSWORD, read_secret


async def main() -> None:
    settings = DashboardSettings()
    client = redis_client(settings, read_secret(REDIS_PASSWORD))
    raw = await client.get(f"{settings.trading_env}:snapshot:positions")
    await client.aclose()
    print(json.dumps(json.loads(raw), indent=2) if raw else "no positions snapshot")


asyncio.run(main())

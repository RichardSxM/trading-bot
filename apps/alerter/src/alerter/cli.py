"""The ``alerter`` command: run the service, or ``alerter test [--tier T]`` (spec 0013 AC-17).

``alerter test`` sends one marked test push per tier (or the one tier given) through the same
delivery path as real alerts, stores the rows, prints each response, and exits 1 when a push
failed.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence

import httpx
from sqlalchemy.exc import SQLAlchemyError

from alerter.delivery import Deliverer
from alerter.main import deliverer, repository, serve
from alerter.service import utc_now
from alerter.settings import AlerterSettings, alerter_secrets, engine_config
from alerter.store import EpisodeStore
from trading_core.alerts.engine import AlertEngine
from trading_core.alerts.model import Delivery, Tier
from trading_core.boot import boot
from trading_core.recording.errors import RecordingError

# Loudest first, so the critical one is on the lock screen by the time you look.
ALL_TIERS = (Tier.CRITICAL, Tier.WARNING, Tier.INFO)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alerter", description="Phone alerts (spec 0013).")
    commands = parser.add_subparsers(dest="command")
    test = commands.add_parser("test", help="send one marked test push per tier")
    test.add_argument("--tier", choices=[t.value for t in Tier], help="send only this tier")
    return parser


async def send_tests(engine: AlertEngine, send: Deliverer, store: EpisodeStore | None, tiers: Sequence[Tier]) -> int:
    """Send the test pushes one by one and print a JSON line per push; returns the exit status."""
    decision = engine.test_episodes(str(uuid.uuid4()), tiers, utc_now())
    status = 0
    for push in decision.pushes:
        outcome = await send.send(push)
        episode = engine.record(push, outcome.delivery, utc_now()) or push.episode
        stored = True
        if store is not None:
            try:
                await asyncio.to_thread(store.save, episode)
            except SQLAlchemyError, RecordingError:
                stored = False
        if outcome.delivery is Delivery.FAILED:
            status = 1
        line = {
            "tier": push.tier.value,
            "delivery": outcome.delivery.value,
            "stored": stored,
            "response": outcome.detail,
        }
        print(json.dumps(line), flush=True)
    return status


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of the ``alerter`` script."""
    args = _parser().parse_args(argv)
    started = boot("worker", AlerterSettings, secrets=alerter_secrets)
    if args.command != "test":
        asyncio.run(serve(started))
        return
    tiers = [Tier(args.tier)] if args.tier else list(ALL_TIERS)
    settings, secrets = started.settings, started.secrets

    async def run() -> int:
        async with httpx.AsyncClient() as http:
            return await send_tests(
                AlertEngine(engine_config(settings)),
                deliverer(settings, secrets, http),
                repository(settings, secrets),
                tiers,
            )

    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()

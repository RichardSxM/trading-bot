"""Records control commands in Postgres ``control_commands`` for the trader (spec 0005 AC-6).

Each call runs the sync repository in a worker thread with its own session, so the node's
event loop never waits on the database.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from trading_core.control.messages import ControlMessage, ControlResult
from trading_core.recording import queries, repository
from trading_core.recording.enums import Env
from trading_core.recording.rows import ControlCommandRow

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class PostgresCommandRecorder:
    """A ``CommandRecorder`` over the trader's database login."""

    def __init__(self, engine: Engine, env: Env) -> None:
        self._engine = engine
        self._env = env

    async def known(self, command_id: uuid.UUID) -> bool:
        """True when ``command_id`` is already in ``control_commands``."""
        return await asyncio.to_thread(self._known, command_id)

    async def received(self, msg: ControlMessage, at: datetime) -> None:
        """Insert ``msg`` as ``received`` (a no-op when it is already there)."""
        await asyncio.to_thread(self._received, msg, at)

    async def finished(self, result: ControlResult) -> None:
        """Set the final outcome, with the reason in ``details``."""
        await asyncio.to_thread(self._finished, result)

    def _known(self, command_id: uuid.UUID) -> bool:
        with Session(self._engine) as session:
            return queries.control_command_known(session, command_id)

    def _received(self, msg: ControlMessage, at: datetime) -> None:
        row = ControlCommandRow(
            id=msg.id,
            env=self._env,
            cmd=msg.cmd,
            issued_at=msg.issued_at,
            expires_at=msg.expires_at,
            received_at=at,
        )
        with Session(self._engine) as session:
            repository.record_control_command(session, row)
            session.commit()

    def _finished(self, result: ControlResult) -> None:
        details = {"reason": result.reason} if result.reason is not None else None
        with Session(self._engine) as session:
            repository.set_control_outcome(session, result.id, result.outcome, _ns(result.outcome_at), details)
            session.commit()


def _ns(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000

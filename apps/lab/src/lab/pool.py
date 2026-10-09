"""The unit scheduler (spec 0008 AC-13, AC-16): one spawned process per unit, at most ``workers`` at a time.

Units start in plan order (variants in order, ``base`` first, then segments). Each child runs
one unit and sends its ``UnitResult`` back over a pipe, or the type and message of whatever
escaped it. A child that ends without sending (killed, out of memory) is a ``WorkerDiedError``.
Either way only that unit's variant fails: ``fail_variant`` drops its units not yet started and
terminates its running ones, while the other variants carry on.

The parent reads a child's pipe before it joins the child, so a large result never blocks the
child's exit. Children ignore SIGINT: the parent owns shutdown (``terminate_all``).
"""

import logging
import multiprocessing
import signal
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection, wait
from multiprocessing.process import BaseProcess
from typing import Final

from lab.units import UnitPlan, UnitResult, run_unit
from trading_core.lab.errors import LabError, UnitFailedError, WorkerDiedError
from trading_core.logs import configure_logging

log = logging.getLogger(__name__)

PROCESS: Final = "lab"
# How long a terminated child gets to exit before it is killed.
_TERMINATE_GRACE_S: Final = 5.0

type Target = Callable[[UnitPlan], UnitResult]


@dataclass(frozen=True)
class UnitDone:
    """A unit that finished and sent its result."""

    plan: UnitPlan
    result: UnitResult


@dataclass(frozen=True)
class UnitFailed:
    """A unit whose worker raised or died; its variant fails (AC-13)."""

    plan: UnitPlan
    error: LabError


type Outcome = UnitDone | UnitFailed


def _child(target: Target, plan: UnitPlan, conn: Connection, env: str) -> None:
    """A worker: run one unit and send ``("ok", result)`` or ``("error", type name, message)``."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    configure_logging(process=PROCESS, env=env)
    # The paper loop's recorder logs every fill at INFO; a replay makes thousands.
    logging.getLogger("trading_core.node.recording").setLevel(logging.WARNING)
    try:
        result = target(plan)
    except BaseException as exc:
        log.exception(
            "lab_unit_failed",
            extra={"fields": {"variant": plan.variant, "segment": plan.segment.index, "error": type(exc).__name__}},
        )
        conn.send(("error", type(exc).__name__, str(exc)))
    else:
        conn.send(("ok", result))
    finally:
        conn.close()


@dataclass(frozen=True)
class _Running:
    plan: UnitPlan
    process: BaseProcess
    conn: Connection


class UnitPool:
    """Runs units in spawned workers and yields each one's outcome as it finishes."""

    def __init__(self, plans: Sequence[UnitPlan], workers: int, *, target: Target = run_unit, env: str) -> None:
        if workers < 1:
            raise ValueError(f"workers must be at least 1, not {workers}")
        self._context = multiprocessing.get_context("spawn")
        self._pending = deque(plans)
        self._running: list[_Running] = []
        self._workers = workers
        self._target = target
        self._env = env

    def _start_more(self) -> None:
        while self._pending and len(self._running) < self._workers:
            plan = self._pending.popleft()
            parent, child = self._context.Pipe(duplex=False)
            process = self._context.Process(
                target=_child,
                args=(self._target, plan, child, self._env),
                name=f"lab-{plan.variant}-S{plan.segment.index}",
            )
            process.start()
            child.close()  # the parent keeps only its read end, so a dead child reads as EOF
            self._running.append(_Running(plan, process, parent))

    def outcomes(self) -> Iterator[Outcome]:
        """Start units in plan order and yield each outcome as its worker finishes."""
        self._start_more()
        while self._running:
            ready = set(wait([r.conn for r in self._running] + [r.process.sentinel for r in self._running]))
            for running in list(self._running):
                if running not in self._running:
                    continue  # terminated by fail_variant while an earlier outcome was handled
                if running.conn not in ready and running.process.sentinel not in ready:
                    continue
                outcome = self._collect(running)
                self._start_more()
                yield outcome
            self._start_more()

    def _collect(self, running: _Running) -> Outcome:
        message: tuple[object, ...] | None = None
        try:
            if running.conn.poll():
                message = running.conn.recv()
        except EOFError, OSError:
            message = None
        running.process.join()
        running.conn.close()
        self._running.remove(running)
        if message is not None and message[0] == "ok" and isinstance(message[1], UnitResult):
            return UnitDone(running.plan, message[1])
        if message is not None and message[0] == "error":
            return UnitFailed(running.plan, UnitFailedError(str(message[1]), str(message[2])))
        return UnitFailed(running.plan, WorkerDiedError(running.process.exitcode))

    def fail_variant(self, variant: str) -> None:
        """Drop ``variant``'s units not yet started and terminate its running ones (AC-13)."""
        self._pending = deque(p for p in self._pending if p.variant != variant)
        for running in [r for r in self._running if r.plan.variant == variant]:
            self._stop(running)

    def terminate_all(self) -> None:
        """Drop every unit not yet started and terminate every running one (SIGINT or SIGTERM, AC-13)."""
        self._pending.clear()
        for running in list(self._running):
            self._stop(running)

    def _stop(self, running: _Running) -> None:
        running.process.terminate()
        running.process.join(_TERMINATE_GRACE_S)
        if running.process.is_alive():
            running.process.kill()
            running.process.join()
        running.conn.close()
        self._running.remove(running)

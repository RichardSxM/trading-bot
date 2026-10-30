"""covers: spec 0005 AC-6, the trader's control stream reader (received then one outcome, duplicates,
expiry, bad entries, Postgres and Redis trouble)."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

# redis comes with the trader image, not the core one the migrate service runs integration tests in.
pytest.importorskip("redis")

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from trading_core.control.consumer import ControlConsumer, Outcome
from trading_core.control.messages import ControlMessage, pause_command, result_from_fields, stop_command, to_fields
from trading_core.recording.enums import ControlCmd, ControlOutcome, ControlSource

NOW = datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
EXECUTED = Outcome(ControlOutcome.EXECUTED)


class FakeRedis:
    """One stream with one consumer group: new entries go pending when read, XACK clears them."""

    def __init__(self):
        self.entries = []  # (entry id, fields)
        self.delivered = 0
        self.pending = {}
        self.results = []
        self.acked = []
        self.calls = []
        self.group_exists = False
        self.fail_next = None

    def add(self, fields):
        entry_id = f"{len(self.entries) + 1}-0"
        self.entries.append((entry_id, {k.encode(): v.encode() for k, v in fields.items()}))
        return entry_id

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.fail_next == name:
            self.fail_next = None
            raise RedisConnectionError("redis down")

    async def xgroup_create(self, name, groupname, id, mkstream):
        self._maybe_fail("xgroup_create")
        if self.group_exists:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.group_exists = True

    async def xreadgroup(self, groupname, consumername, streams, count, block):
        self._maybe_fail("xreadgroup")
        ((name, start),) = streams.items()
        if start == "0":
            found = list(self.pending.items())[:count]
        else:
            found = self.entries[self.delivered : self.delivered + count]
            self.delivered += len(found)
            self.pending.update(dict(found))
        if not found:
            return []
        return [[name.encode(), [(entry_id.encode(), fields) for entry_id, fields in found]]]

    async def xadd(self, name, fields, id="*", maxlen=None, approximate=True):
        self._maybe_fail("xadd")
        assert name == "demo:control:results"
        self.results.append(result_from_fields(fields))
        self.calls.append(("result", fields["outcome"]))

    async def xack(self, name, groupname, *ids):
        self._maybe_fail("xack")
        for entry_id in ids:
            self.pending.pop(entry_id, None)
            self.acked.append(entry_id)


class FakeRecorder:
    def __init__(self):
        self.rows = {}
        self.fail = False

    async def outcome(self, command_id):
        if self.fail:
            raise OSError("postgres down")
        rows = self.rows.get(command_id)
        return None if rows is None else ControlOutcome(rows[-1])

    async def received(self, msg, at):
        if self.fail:
            raise OSError("postgres down")
        self.rows.setdefault(msg.id, ["received"])  # a no-op when recorded, as in Postgres

    async def finished(self, result):
        if self.fail:
            raise OSError("postgres down")
        self.rows[result.id].append(result.outcome.value)


def _consumer(redis, recorder, executed=None, outcome=EXECUTED, now=NOW, executor=None, **kw):
    if executor is not None:
        return ControlConsumer(redis, "demo", executor, recorder, lambda: now, **kw)

    async def execute(msg):
        if executed is not None:
            executed.append(msg)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return ControlConsumer(redis, "demo", execute, recorder, lambda: now, **kw)


def _drain(consumer):
    async def run():
        while await consumer.step():
            pass
        await consumer.step()  # the switch from pending to new entries

    asyncio.run(run())


def _outcomes(redis, command_id):
    return [r.outcome.value for r in redis.results if r.id == command_id]


def test_a_pause_gets_received_then_executed_is_recorded_and_acked_last():
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    msg = pause_command(NOW, 60, ControlSource.DEPLOY)
    entry_id = redis.add(to_fields(msg))

    _drain(_consumer(redis, recorder, executed))

    assert [m.id for m in executed] == [msg.id]
    assert executed[0].source is ControlSource.DEPLOY
    assert _outcomes(redis, msg.id) == ["received", "executed"]
    assert recorder.rows[msg.id] == ["received", "executed"]
    assert redis.acked == [entry_id]
    assert redis.calls.index("xack") > redis.calls.index(("result", "executed"))


def test_the_group_is_created_once_and_busygroup_is_ignored():
    redis = FakeRedis()
    redis.group_exists = True

    _drain(_consumer(redis, FakeRecorder()))

    assert redis.calls[0] == "xgroup_create"
    assert redis.calls.count("xgroup_create") == 1


def test_a_repeated_id_is_answered_duplicate_and_not_executed_again():
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    msg = pause_command(NOW, 60, ControlSource.DASHBOARD)
    recorder.rows[msg.id] = ["received", "executed"]  # handled by an earlier trader process
    redis.add(to_fields(msg))

    _drain(_consumer(redis, recorder, executed))

    assert executed == []
    assert _outcomes(redis, msg.id) == ["duplicate"]


@pytest.mark.parametrize("make", [stop_command, lambda now, ttl: pause_command(now, ttl, ControlSource.DASHBOARD)])
def test_a_command_left_at_received_by_a_stopped_process_runs_after_the_restart(make):
    # covers: spec 0005 AC-6, review 2026-10-17 major 1: the trader recorded `received`, then was
    # stopped before it ran the command; the entry is still pending, so the new process reads it again.
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    msg = make(NOW, 60)
    recorder.rows[msg.id] = ["received"]
    entry_id = redis.add(to_fields(msg))

    _drain(_consumer(redis, recorder, executed))

    assert [m.id for m in executed] == [msg.id]
    assert _outcomes(redis, msg.id) == ["received", "executed"]
    assert recorder.rows[msg.id] == ["received", "executed"]
    assert redis.acked == [entry_id]


def test_a_command_left_at_received_that_expired_meanwhile_is_answered_expired():
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    msg = stop_command(NOW - timedelta(seconds=61), 60)
    recorder.rows[msg.id] = ["received"]
    redis.add(to_fields(msg))

    _drain(_consumer(redis, recorder, executed))

    assert executed == []
    assert _outcomes(redis, msg.id) == ["expired"]


def test_an_expired_command_is_acknowledged_as_expired_and_not_executed():
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    msg = pause_command(NOW - timedelta(seconds=61), 60, ControlSource.DEPLOY)
    entry_id = redis.add(to_fields(msg))

    _drain(_consumer(redis, recorder, executed))

    assert executed == []
    assert _outcomes(redis, msg.id) == ["expired"]
    assert recorder.rows[msg.id] == ["received", "expired"]
    assert redis.acked == [entry_id]


def test_a_failed_outcome_carries_its_reason():
    redis, recorder = FakeRedis(), FakeRecorder()
    msg = stop_command(NOW, 60)
    redis.add(to_fields(msg))

    _drain(_consumer(redis, recorder, outcome=Outcome(ControlOutcome.FAILED, "not_implemented")))

    assert [(r.outcome.value, r.reason) for r in redis.results] == [("received", None), ("failed", "not_implemented")]


def test_a_bad_entry_gets_failed_and_a_warning_and_is_acked(caplog):
    redis = FakeRedis()
    command_id = uuid.uuid4()
    entry_id = redis.add({"id": str(command_id), "cmd": "launch", "schema_version": "1"})

    with caplog.at_level(logging.WARNING):
        _drain(_consumer(redis, FakeRecorder()))

    assert [(r.id, r.outcome.value, r.reason) for r in redis.results] == [(command_id, "failed", "invalid")]
    assert [c for r in caplog.records if (c := getattr(r, "fields", {}).get("code"))] == ["control_entry_invalid"]
    assert redis.acked == [entry_id]


def test_a_bad_entry_without_an_id_is_only_logged_and_acked():
    redis = FakeRedis()
    entry_id = redis.add({"cmd": "pause"})

    _drain(_consumer(redis, FakeRecorder()))

    assert redis.results == []
    assert redis.acked == [entry_id]


def test_a_postgres_error_logs_and_never_blocks_the_command(caplog):
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    recorder.fail = True
    msg = pause_command(NOW, 60, ControlSource.DEPLOY)
    redis.add(to_fields(msg))

    with caplog.at_level(logging.ERROR):
        _drain(_consumer(redis, recorder, executed))

    assert len(executed) == 1
    assert _outcomes(redis, msg.id) == ["received", "executed"]
    codes = {getattr(r, "fields", {}).get("code") for r in caplog.records} - {None}
    assert codes == {"control_record_failed"}


def test_an_executor_bug_answers_failed_and_the_reader_goes_on(caplog):
    redis = FakeRedis()
    msg = pause_command(NOW, 60, ControlSource.DEPLOY)
    redis.add(to_fields(msg))

    with caplog.at_level(logging.ERROR):
        _drain(_consumer(redis, FakeRecorder(), outcome=RuntimeError("bug")))

    assert [(r.outcome.value, r.reason) for r in redis.results][-1] == ("failed", "error")
    assert "control_execute_failed" in {getattr(r, "fields", {}).get("code") for r in caplog.records}


def test_an_entry_handled_but_not_acked_gets_the_same_result_again_after_redis_returns():
    redis, recorder = FakeRedis(), FakeRecorder()
    executed: list[ControlMessage] = []
    msg = pause_command(NOW, 60, ControlSource.DEPLOY)
    entry_id = redis.add(to_fields(msg))
    consumer = _consumer(redis, recorder, executed)

    async def run():
        await consumer.step()  # pending pass: nothing yet, switch to new entries
        redis.fail_next = "xack"
        with pytest.raises(RedisConnectionError):
            await consumer.step()
        consumer._redis_failed(RedisConnectionError("redis down"))  # what run() does on the error
        while await consumer.step():
            pass

    asyncio.run(run())

    assert len(executed) == 1
    assert _outcomes(redis, msg.id) == ["received", "executed", "executed"]
    assert entry_id in redis.acked
    assert redis.pending == {}


def test_a_redis_outage_warns_at_most_once_a_minute_and_retries(caplog):
    class DownRedis(FakeRedis):
        async def xreadgroup(self, *args, **kwargs):
            raise RedisConnectionError("redis down")

    redis = DownRedis()
    clock = [0.0]

    def monotonic():
        clock[0] += 10.0
        return clock[0]

    consumer = _consumer(redis, FakeRecorder(), monotonic=monotonic, retry_s=0.001)

    async def run():
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())

    warnings = [r for r in caplog.records if getattr(r, "fields", {}).get("code") == "control_read_failed"]
    attempts = clock[0] / 10.0
    assert attempts > 12
    assert len(warnings) == pytest.approx(attempts // 6 + 1, abs=1)


def test_every_command_kind_reaches_the_executor():
    redis = FakeRedis()
    executed: list[ControlMessage] = []
    for cmd in ControlCmd:
        msg = pause_command(NOW, 60, ControlSource.DEPLOY)
        redis.add(to_fields(msg) | {"cmd": cmd.value})

    _drain(_consumer(redis, FakeRecorder(), executed))

    assert [m.cmd for m in executed] == list(ControlCmd)


def test_a_second_stop_waits_until_the_first_has_its_outcome():
    """covers: spec 0010 AC-3 (a second command waits its turn)"""
    redis = FakeRedis()
    first, second = stop_command(NOW, 60), stop_command(NOW, 60)
    redis.add(to_fields(first))
    redis.add(to_fields(second))
    steps = []

    async def execute(msg):
        steps.append(("start", msg.id))
        await asyncio.sleep(0.01)  # a flatten waiting for flat yields to the loop
        steps.append(("end", msg.id))
        return EXECUTED

    _drain(_consumer(redis, FakeRecorder(), executor=execute))

    assert steps == [("start", first.id), ("end", first.id), ("start", second.id), ("end", second.id)]
    assert [(r.id, r.outcome.value) for r in redis.results if r.outcome.value == "executed"] == [
        (first.id, "executed"),
        (second.id, "executed"),
    ]


def test_an_outcome_carries_its_details_to_the_result():
    # covers: spec 0023 AC-20, recheck answers with its outcome and report id.
    redis, recorder = FakeRedis(), FakeRecorder()
    msg = stop_command(NOW, 60)
    redis.add(to_fields(msg))
    details = {"outcome": "clean", "reconciliation_id": "01a18814-ccfb-72a2-a151-89af16daccc4"}

    _drain(_consumer(redis, recorder, outcome=Outcome(ControlOutcome.EXECUTED, None, details)))

    assert [r.details for r in redis.results] == [None, details]

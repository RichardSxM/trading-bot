"""covers: spec 0013 AC-12 (mute holds warnings until it ends, never criticals, then one summary; a new mute
replaces the old; unmute ends it), AC-16 (the commands' results, with ``until``; a test sends one push per
tier), and the API surface (expired and invalid commands)."""

import asyncio
import json
import uuid
from datetime import timedelta

from alerter.commands import MuteKey
from alerter.service import Alerter
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, HoldKind, Observation, PushReason, Tier
from trading_core.control.alerts import AlertCommand, alert_command, command_to_fields, result_from_fields
from trading_core.recording.enums import AlertCmd, ControlOutcome

from .conftest import NOW, Clock, FakeDeliverer, FakeStore, FakeStreams

STREAM = "demo:control:alerts"
RESULTS = "demo:control:alerts:results"
MUTE_KEY = "demo:alerts:mute"


def make(tmp_path, delivery=Delivery.DRY_RUN):
    clock = Clock()
    engine = AlertEngine(EngineConfig(env="demo"))
    redis = FakeStreams()
    deliverer = FakeDeliverer(delivery)
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=StoreWriter(FakeStore(), engine, clock, retry_s=0),
        redis=redis,
        trader_down_s=45,
        clock=clock,
        liveness_file=tmp_path / "alive",
        control=redis,
        mute=MuteKey(redis, "demo"),
    )
    return alerter, clock, redis, deliverer, engine


async def settle(alerter):
    while alerter._pushes:
        await asyncio.gather(*list(alerter._pushes))


async def send(alerter, redis, clock, cmd, mute_s=None, *, at=None, fields=None):
    msg = alert_command(cmd, at or clock(), 30, mute_s=mute_s)
    redis.append(STREAM, fields or command_to_fields(msg))
    await alerter._commands.step()
    await alerter._commands.step()
    await alerter._commands.drain()
    await settle(alerter)
    return msg


def results(redis):
    return [result_from_fields(fields) for _, fields in redis.streams.get(RESULTS, [])]


def line(code, level, at):
    return Observation("log:trader", "trader", at, code, level, {"code": code, "msg": "something"})


def test_a_mute_holds_a_warning_but_never_a_critical_and_answers_with_until(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, engine = make(tmp_path)
        msg = await send(alerter, redis, clock, AlertCmd.MUTE, 3600)
        alerter.apply(engine.observe(line("kraken_key_rotation_due", "WARNING", clock())))
        alerter.apply(engine.observe(line("kraken_auth_failed", "CRITICAL", clock())))
        await settle(alerter)
        return msg, redis, deliverer, engine

    msg, redis, deliverer, engine = asyncio.run(run())

    (result,) = results(redis)
    assert (result.id, result.cmd, result.outcome) == (msg.id, AlertCmd.MUTE, ControlOutcome.EXECUTED)
    assert result.until == msg.issued_at + timedelta(hours=1)
    stored = json.loads(redis.values[MUTE_KEY])
    assert (stored["command_id"], redis.ttls[MUTE_KEY]) == (str(msg.id), 3600)
    assert [p.tier for p in deliverer.sent] == [Tier.CRITICAL]
    held = {e.fingerprint: e.delivery for e in engine.open_episodes}
    assert held["kraken_key_rotation_due"] is Delivery.MUTED
    assert redis.acked[STREAM] == {"1-0"}


def test_an_unmute_ends_the_mute_with_one_summary_and_deletes_the_key(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, engine = make(tmp_path)
        await send(alerter, redis, clock, AlertCmd.MUTE, 14_400)
        alerter.apply(engine.observe(line("kraken_key_rotation_due", "WARNING", clock())))
        clock.advance(60)
        await send(alerter, redis, clock, AlertCmd.UNMUTE)
        return redis, deliverer, engine

    redis, deliverer, engine = asyncio.run(run())

    (summary,) = deliverer.sent
    assert (summary.reason, summary.episode.title) == (PushReason.SUMMARY, "[demo] 1 warning while muted")
    assert MUTE_KEY not in redis.values
    assert engine.holds == ()
    assert [(r.cmd, r.outcome, r.until) for r in results(redis)][1] == (
        AlertCmd.UNMUTE,
        ControlOutcome.EXECUTED,
        None,
    )


def test_a_mute_that_runs_out_sends_its_summary_on_the_next_tick(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, engine = make(tmp_path)
        await send(alerter, redis, clock, AlertCmd.MUTE, 3600)
        alerter.apply(engine.observe(line("kraken_key_rotation_due", "WARNING", clock())))
        clock.advance(3601)
        alerter.tick()
        await settle(alerter)
        return deliverer

    deliverer = asyncio.run(run())

    assert [p.reason for p in deliverer.sent] == [PushReason.SUMMARY]


def test_a_new_mute_replaces_the_current_one(tmp_path):
    async def run():
        alerter, clock, redis, _, engine = make(tmp_path)
        await send(alerter, redis, clock, AlertCmd.MUTE, 43_200)
        second = await send(alerter, redis, clock, AlertCmd.MUTE, 3600)
        return second, redis, engine

    second, redis, engine = asyncio.run(run())

    (hold,) = engine.holds
    assert (hold.kind, hold.id, hold.until) == (HoldKind.MUTE, f"mute:{second.id}", NOW + timedelta(hours=1))
    assert json.loads(redis.values[MUTE_KEY])["command_id"] == str(second.id)


def test_an_expired_command_does_nothing_and_says_expired(tmp_path):
    async def run():
        alerter, clock, redis, _, engine = make(tmp_path)
        await send(alerter, redis, clock, AlertCmd.MUTE, 3600, at=NOW - timedelta(minutes=5))
        return redis, engine

    redis, engine = asyncio.run(run())

    assert [r.outcome for r in results(redis)] == [ControlOutcome.EXPIRED]
    assert (engine.holds, MUTE_KEY in redis.values) == ((), False)


def test_a_mute_of_another_length_fails_invalid(tmp_path):
    async def run():
        alerter, clock, redis, _, engine = make(tmp_path)
        fields = command_to_fields(alert_command(AlertCmd.MUTE, clock(), 30, mute_s=3600)) | {"mute_s": "999999"}
        await send(alerter, redis, clock, AlertCmd.MUTE, 3600, fields=fields)
        return redis, engine

    redis, engine = asyncio.run(run())

    (result,) = results(redis)
    assert (result.cmd, result.outcome, result.reason) == (AlertCmd.MUTE, ControlOutcome.FAILED, "invalid")
    assert engine.holds == ()
    assert redis.acked[STREAM] == {"1-0"}


def test_a_test_command_sends_one_push_per_tier_even_while_muted(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, _ = make(tmp_path)
        await send(alerter, redis, clock, AlertCmd.MUTE, 3600)
        msg = await send(alerter, redis, clock, AlertCmd.TEST)
        return msg, redis, deliverer

    msg, redis, deliverer = asyncio.run(run())

    assert [(p.reason, p.tier) for p in deliverer.sent] == [
        (PushReason.TEST, Tier.CRITICAL),
        (PushReason.TEST, Tier.WARNING),
        (PushReason.TEST, Tier.INFO),
    ]
    assert {p.episode.fingerprint for p in deliverer.sent} == {
        f"test:{msg.id}:{t}" for t in ("critical", "warning", "info")
    }
    assert results(redis)[-1].outcome is ControlOutcome.EXECUTED


def test_a_test_whose_push_fails_answers_push_failed(tmp_path):
    async def run():
        alerter, clock, redis, _, _ = make(tmp_path, delivery=Delivery.FAILED)
        await send(alerter, redis, clock, AlertCmd.TEST)
        return redis

    (result,) = results(asyncio.run(run()))

    assert (result.outcome, result.reason) == (ControlOutcome.FAILED, "push_failed")


def test_a_restart_holds_again_under_the_stored_mute(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, engine = make(tmp_path)
        until = NOW + timedelta(hours=2)
        await MuteKey(redis, "demo").set(until, alert_command(AlertCmd.UNMUTE, NOW, 30).id, NOW)
        await alerter.restore_mute()
        alerter.apply(engine.observe(line("kraken_key_rotation_due", "WARNING", clock())))
        await settle(alerter)
        return deliverer, engine, until

    deliverer, engine, until = asyncio.run(run())

    assert deliverer.sent == []
    ((hold),) = engine.holds
    assert (hold.kind, hold.until) == (HoldKind.MUTE, until)


def test_a_stored_mute_already_past_is_ignored(tmp_path):
    async def run():
        alerter, _, redis, _, engine = make(tmp_path)
        await MuteKey(redis, "demo").set(NOW - timedelta(seconds=1), alert_command(AlertCmd.TEST, NOW, 30).id, NOW)
        await alerter.restore_mute()
        return engine

    assert asyncio.run(run()).holds == ()


def test_a_mute_read_late_still_ends_at_its_issued_at_plus_its_length(tmp_path):
    async def run():
        alerter, clock, redis, _, _ = make(tmp_path)
        msg = await send(alerter, redis, clock, AlertCmd.MUTE, 3600, at=clock() - timedelta(seconds=20))
        return msg, redis

    msg, redis = asyncio.run(run())

    (result,) = results(redis)
    assert result.until == msg.issued_at + timedelta(hours=1)
    assert json.loads(redis.values[MUTE_KEY])["command_id"] == str(msg.id)
    assert redis.ttls[MUTE_KEY] == 3580


def test_a_second_test_within_30_s_answers_too_soon(tmp_path):
    async def run():
        alerter, clock, redis, deliverer, _ = make(tmp_path)
        await send(alerter, redis, clock, AlertCmd.TEST)
        clock.advance(29)
        await send(alerter, redis, clock, AlertCmd.TEST)
        clock.advance(1)
        await send(alerter, redis, clock, AlertCmd.TEST)
        return redis, deliverer

    redis, deliverer = asyncio.run(run())

    assert [(r.outcome, r.reason) for r in results(redis)] == [
        (ControlOutcome.EXECUTED, None),
        (ControlOutcome.FAILED, "too_soon"),
        (ControlOutcome.EXECUTED, None),
    ]
    assert len(deliverer.sent) == 6


class SlowDeliverer(FakeDeliverer):
    """Holds every push until ``release`` is set, like Pushover retrying for minutes."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()

    async def send(self, push):
        await self.release.wait()
        return await super().send(push)


def test_a_mute_behind_a_slow_test_is_answered_first(tmp_path):
    async def run():
        alerter, clock, redis, _, engine = make(tmp_path)
        slow = SlowDeliverer()
        alerter._deliverer = slow
        test = alert_command(AlertCmd.TEST, clock(), 30)
        redis.append(STREAM, command_to_fields(test))
        await alerter._commands.step()
        await alerter._commands.step()
        await asyncio.sleep(0)
        mute = await send_without_drain(alerter, redis, clock, AlertCmd.MUTE, 3600)
        answered_first = [r.id for r in results(redis)]
        held = engine.holds
        slow.release.set()
        await alerter._commands.drain()
        return answered_first, mute, test, held, redis

    answered_first, mute, test, held, redis = asyncio.run(run())

    assert answered_first == [mute.id]
    assert [h.kind for h in held] == [HoldKind.MUTE]
    assert [r.id for r in results(redis)] == [mute.id, test.id]
    assert redis.acked[STREAM] == {"1-0", "2-0"}


async def send_without_drain(alerter, redis, clock, cmd, mute_s=None):
    msg = alert_command(cmd, clock(), 30, mute_s=mute_s)
    redis.append(STREAM, command_to_fields(msg))
    await alerter._commands.step()
    return msg


def test_a_mute_without_its_length_fails_closed(tmp_path):
    async def run():
        alerter, clock, _, _, engine = make(tmp_path)
        msg = AlertCommand(uuid.uuid4(), AlertCmd.MUTE, clock(), clock() + timedelta(seconds=30))
        return await alerter.execute(msg), engine

    result, engine = asyncio.run(run())

    assert (result.outcome, result.reason) == (ControlOutcome.FAILED, "invalid")
    assert engine.holds == ()

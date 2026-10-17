"""covers: spec 0013 AC-2 (the host read gates the ping), AC-4 (host lines, ``disk_low``), AC-6
(entry blocks from ``trader_state``), and AC-11 (the deploy window from the real files)."""

import asyncio
import json
import os
from datetime import timedelta
from decimal import Decimal

from alerter.service import Alerter
from alerter.sources.host_files import DEPLOYS_FILE, HOST_FILE, FileFollower, HostFiles, parse
from alerter.store import StoreWriter
from trading_core.alerts.engine import AlertEngine, EngineConfig
from trading_core.alerts.model import Delivery, Observation, PushReason, Tier
from trading_core.snapshot.envelope import encode

from .conftest import NOW, Clock, FakeDeliverer, FakeRedis, FakeStore

SHA = "4d55abd0123456789"
STATE = "demo:snapshot:trader_state"


def append(path, *lines):
    with path.open("a") as file:
        for line in lines:
            file.write(line if isinstance(line, str) else json.dumps(line) + "\n")


def ns(at):
    return int(at.timestamp()) * 1_000_000_000


def make(tmp_path, *, host=True, disk_free_gb=Decimal(5), watchdog=None, entry_block_s=3600):
    clock = Clock()
    engine = AlertEngine(EngineConfig(env="demo", entry_block_s=entry_block_s))
    redis = FakeRedis()
    deliverer = FakeDeliverer()
    logs = tmp_path / "kfs-log"
    logs.mkdir()
    alerter = Alerter(
        env="demo",
        engine=engine,
        deliverer=deliverer,
        store=StoreWriter(FakeStore(), engine, clock, retry_s=0),
        redis=redis,
        trader_down_s=45,
        watchdog=watchdog,
        host=HostFiles(logs, clock) if host else None,
        disk_path=logs if host else None,
        disk_free_gb=disk_free_gb,
        clock=clock,
        liveness_file=tmp_path / "alive",
    )
    return alerter, clock, deliverer, redis, logs


async def settle(alerter):
    while alerter._pushes:
        await asyncio.gather(*list(alerter._pushes))


# FileFollower ---------------------------------------------------------------------------------


def test_a_file_is_followed_from_its_end_when_first_seen(tmp_path):
    path = tmp_path / "f.jsonl"
    append(path, "old 1\n", "old 2\n")
    follower = FileFollower(path)

    assert follower.read() == []
    append(path, "new\n")
    assert follower.read() == ["new"]
    assert follower.read() == []


def test_a_line_still_being_written_waits_for_its_newline(tmp_path):
    path = tmp_path / "f.jsonl"
    path.touch()
    follower = FileFollower(path)
    follower.read()

    append(path, "par")
    assert follower.read() == []
    append(path, "tial\nnext\n")
    assert follower.read() == ["partial", "next"]


def test_a_truncated_file_is_read_again_from_its_start(tmp_path):
    path = tmp_path / "f.jsonl"
    append(path, "a long first line that copytruncate will cut\n")
    follower = FileFollower(path)
    follower.read()

    path.write_text("")  # copytruncate
    append(path, "after\n")
    assert follower.read() == ["after"]


def test_a_replaced_file_is_read_from_its_start(tmp_path):
    path = tmp_path / "f.jsonl"
    append(path, "old\n")
    follower = FileFollower(path)
    follower.read()

    os.rename(path, tmp_path / "f.jsonl.1")
    append(path, "fresh one\n", "fresh two\n")
    assert follower.read() == ["fresh one", "fresh two"]


def test_a_file_that_appears_later_is_read_from_its_start(tmp_path):
    path = tmp_path / "f.jsonl"
    follower = FileFollower(path)

    assert follower.read() == []
    append(path, "first\n")
    assert follower.read() == ["first"]


def test_host_and_deploy_lines_parse_with_their_own_times():
    host = parse("host", json.dumps({"ts": "2026-10-16T02:59:58Z", "level": "INFO", "msg": "deploy started"}), NOW)
    deploy = parse("deploy", json.dumps({"finished_at": "2026-10-16T02:59:59Z", "outcome": "deployed"}), NOW)
    future = parse("host", json.dumps({"ts": "2026-10-16T04:00:00Z", "msg": "x"}), NOW)

    assert host is not None
    assert (host.source, host.service, host.code, host.level) == ("host", "host", None, "INFO")
    assert host.at == NOW - timedelta(seconds=2)
    assert deploy is not None
    assert deploy.at == NOW - timedelta(seconds=1)
    assert future is not None
    assert future.at == NOW  # a clock ahead of the alerter's counts as now
    assert parse("host", "not json", NOW) is None
    assert parse("host", "[1, 2]", NOW) is None


# The service ----------------------------------------------------------------------------------


def test_a_host_critical_line_pushes(tmp_path):
    async def run():
        alerter, _, deliverer, _, logs = make(tmp_path)
        await alerter.read_host()  # the first look starts at the end
        append(
            logs / HOST_FILE,
            {
                "ts": "2026-10-16T03:00:00Z",
                "level": "CRITICAL",
                "msg": "autoheal_gave_up",
                "container": "kfs-recorder-1",
                "restarts_last_hour": "3",
            },
        )
        await alerter.read_host()
        await settle(alerter)
        return deliverer

    deliverer = asyncio.run(run())

    [push] = deliverer.sent
    assert (push.episode.fingerprint, push.tier) == ("autoheal_gave_up:kfs-recorder-1", Tier.CRITICAL)
    assert push.episode.source == "host"


def warning_line(at):
    return Observation(
        "log:trader", "trader", at, "kraken_key_rotation_due", "WARNING", {"code": "kraken_key_rotation_due"}
    )


def test_a_deploy_window_from_the_files_holds_a_warning_into_one_summary(tmp_path):
    async def run():
        alerter, clock, deliverer, _, logs = make(tmp_path)
        (logs / DEPLOYS_FILE).touch()
        await alerter.read_host()
        started = {
            "ts": "2026-10-16T03:00:00Z",
            "level": "INFO",
            "msg": "deploy started",
            "sha": SHA,
            "previous_sha": "aaa",
            "started_at": "2026-10-16T03:00:00Z",
        }
        append(logs / HOST_FILE, started)
        await alerter.read_host()
        clock.advance(60)
        alerter.apply(alerter._engine.observe(warning_line(clock())))
        await settle(alerter)
        during = list(deliverer.sent)
        clock.advance(120)
        append(
            logs / DEPLOYS_FILE,
            {
                "sha": SHA,
                "previous_sha": "aaa",
                "started_at": "2026-10-16T03:00:00Z",
                "finished_at": "2026-10-16T03:03:00Z",
                "outcome": "deployed",
            },
        )
        await alerter.read_host()
        await settle(alerter)
        return alerter, during, deliverer

    alerter, during, deliverer = asyncio.run(run())

    assert during == []
    [summary] = deliverer.sent
    assert summary.reason is PushReason.SUMMARY
    assert summary.title == "[demo] 1 warning during deploy"
    assert alerter._engine.holds == ()


def started_line(at):
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"ts": stamp, "level": "INFO", "msg": "deploy started", "sha": SHA, "started_at": stamp}


def outcome_line(started, finished):
    return {
        "sha": SHA,
        "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "outcome": "deployed",
    }


def test_an_alerter_recreated_mid_deploy_opens_the_window_from_the_start_line(tmp_path):
    async def run():
        alerter, clock, deliverer, _, logs = make(tmp_path)
        began = clock() - timedelta(minutes=2)
        crit = {"ts": "2026-10-16T02:00:00Z", "level": "CRITICAL", "msg": "autoheal_gave_up", "container": "x"}
        append(logs / HOST_FILE, crit, started_line(began))
        (logs / DEPLOYS_FILE).touch()
        await alerter.read_host()
        holds = alerter._engine.holds
        alerter.apply(alerter._engine.observe(warning_line(clock())))
        await settle(alerter)
        during = list(deliverer.sent)
        clock.advance(60)
        append(logs / DEPLOYS_FILE, outcome_line(began, clock()))
        await alerter.read_host()
        await settle(alerter)
        return holds, during, deliverer, alerter

    holds, during, deliverer, alerter = asyncio.run(run())

    [hold] = holds
    assert hold.started_at == NOW - timedelta(minutes=2)
    assert hold.until == NOW + timedelta(minutes=13)  # 15 min from the deploy's start
    assert during == []  # neither the old critical line nor the warning
    [summary] = deliverer.sent
    assert summary.title == "[demo] 1 warning during deploy"
    assert alerter._engine.holds == ()


def test_a_finished_or_old_deploy_opens_no_window_at_start(tmp_path):
    async def run(name, outcome, minutes_ago):
        alerter, clock, _, _, logs = make(tmp_path / name)
        began = clock() - timedelta(minutes=minutes_ago)
        append(logs / HOST_FILE, started_line(began))
        (logs / DEPLOYS_FILE).touch()
        if outcome:
            append(logs / DEPLOYS_FILE, outcome_line(began, clock()))
        await alerter.read_host()
        return alerter._engine.holds

    for name, outcome, minutes_ago in (("done", True, 2), ("old", False, 15)):
        (tmp_path / name).mkdir()
        assert asyncio.run(run(name, outcome, minutes_ago)) == (), name


def test_the_first_look_back_reads_only_the_tail(tmp_path):
    path = tmp_path / "f.jsonl"
    append(path, "x" * (100 << 10) + "\n", "last\n", "partial")
    follower = FileFollower(path)

    assert follower.read() == []
    assert follower.tail == ["last"]


def test_no_host_directory_means_no_host_reads_and_no_disk_check(tmp_path):
    async def run():
        alerter, _, deliverer, _, _ = make(tmp_path, host=False)
        await alerter.read_host()
        await alerter.check_disk()
        await settle(alerter)
        return deliverer

    assert asyncio.run(run()).sent == []


def test_low_disk_pushes_critical(tmp_path):
    async def run():
        # No test filesystem has a petabyte free.
        alerter, _, deliverer, _, _ = make(tmp_path, disk_free_gb=Decimal(10**6))
        await alerter.check_disk()
        await settle(alerter)
        return deliverer

    [push] = asyncio.run(run()).sent
    assert (push.episode.rule, push.tier) == ("disk_low", Tier.CRITICAL)
    assert "floor is 1000000 GB" in push.message


def test_plenty_of_disk_pushes_nothing(tmp_path):
    async def run():
        alerter, _, deliverer, _, _ = make(tmp_path, disk_free_gb=Decimal("0.001"))
        await alerter.check_disk()
        await settle(alerter)
        return deliverer

    assert asyncio.run(run()).sent == []


def state(at, blocked, since):
    return encode(ns(at), {"paused": False, "entries_blocked": blocked, "entries_blocked_since": since})


def test_a_stuck_entry_block_from_trader_state_pushes_a_warning(tmp_path):
    async def run():
        alerter, clock, deliverer, redis, _ = make(tmp_path)
        redis.values[STATE] = state(clock(), ["stale_feed"], {"stale_feed": ns(clock() - timedelta(hours=2))})
        await alerter.check_entry_blocks()
        await settle(alerter)
        return deliverer

    [push] = asyncio.run(run()).sent
    assert (push.episode.fingerprint, push.tier) == ("entry_block:stale_feed", Tier.WARNING)
    assert push.message == "No fresh prices from Kraken (stale_feed)."


def test_a_stale_or_missing_trader_state_changes_nothing(tmp_path):
    async def run():
        alerter, clock, _, redis, _ = make(tmp_path, entry_block_s=60)
        redis.values[STATE] = state(clock(), ["stale_feed"], {"stale_feed": ns(clock())})
        await alerter.check_entry_blocks()
        opened = alerter._engine.open_episodes
        # The trader stops: its last state ages past the threshold, still naming the reason.
        clock.advance(46)
        redis.values[STATE] = state(clock() - timedelta(seconds=46), [], {})
        await alerter.check_entry_blocks()
        still = alerter._engine.open_episodes
        del redis.values[STATE]
        await alerter.check_entry_blocks()
        redis.fail = True
        await alerter.check_entry_blocks()
        return opened, still, alerter._engine.open_episodes

    opened, still, after = asyncio.run(run())
    assert [e.fingerprint for e in opened] == ["entry_block:stale_feed"]
    assert still == after == opened


def test_a_cleared_reason_resolves(tmp_path):
    async def run():
        alerter, clock, _, redis, _ = make(tmp_path)
        redis.values[STATE] = state(clock(), ["exit_failed"], {"exit_failed": ns(clock())})
        await alerter.check_entry_blocks()
        clock.advance(5)
        redis.values[STATE] = state(clock(), [], {})
        await alerter.check_entry_blocks()
        return alerter._engine.open_episodes

    assert asyncio.run(run()) == ()


class FakeWatchdog:
    def __init__(self) -> None:
        self.pings: list[bool] = []

    async def ping(self, *, failing: bool) -> bool:
        self.pings.append(failing)
        return True


def test_the_ping_waits_for_a_fresh_host_read(tmp_path):
    async def run():
        watchdog = FakeWatchdog()
        alerter, clock, _, _, _ = make(tmp_path, watchdog=watchdog)
        await alerter.check_heartbeat()
        await alerter.ping()  # no host read yet
        await alerter.read_host()
        await alerter.ping()
        clock.advance(61)
        await alerter.check_heartbeat()
        await alerter.ping()  # the host read is a minute old
        return watchdog

    assert asyncio.run(run()).pings == [False]


def test_a_host_read_error_logs_and_keeps_going(tmp_path, monkeypatch):
    async def run():
        alerter, _, _, _, _ = make(tmp_path)

        def broken():
            raise PermissionError("denied")

        monkeypatch.setattr(alerter._host, "poll", broken)
        await alerter.read_host()
        return alerter._host.ok_at

    assert asyncio.run(run()) is None


def test_delivery_states_after_a_summary_are_stored(tmp_path):
    async def run():
        alerter, clock, _, _, logs = make(tmp_path)
        await alerter.read_host()
        append(
            logs / HOST_FILE,
            {"ts": "2026-10-16T03:00:00Z", "msg": "deploy started", "sha": SHA, "started_at": "2026-10-16T03:00:00Z"},
        )
        await alerter.read_host()
        alerter.apply(alerter._engine.observe(warning_line(clock())))
        clock.advance(120)
        append(
            logs / DEPLOYS_FILE,
            {
                "sha": SHA,
                "started_at": "2026-10-16T03:00:00Z",
                "finished_at": "2026-10-16T03:02:00Z",
                "outcome": "deployed",
            },
        )
        await alerter.read_host()
        await settle(alerter)
        return alerter

    alerter = asyncio.run(run())
    [episode] = [e for e in alerter._engine.open_episodes if e.rule == "kraken_key_rotation_due"]
    assert (episode.delivery, episode.sends) == (Delivery.DRY_RUN, 1)

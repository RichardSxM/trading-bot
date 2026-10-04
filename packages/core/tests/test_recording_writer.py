"""covers: spec 0006 AC-12 (transient errors retried in order after 1, 2, then every 5 s; the
``recording_unhealthy`` block and when it clears; a conflict is reported, dead lettered, and blocks
``record_conflict``; any other error dead letters and blocks ``recording_failed`` while the rest still
lands; a full queue overflows) and AC-13 (drain, then spill in order).

The repository functions are replaced by recording fakes; ``tests/integration/test_paper_recording.py``
runs the same writer against Postgres.
"""

import itertools
import threading
import time
from contextlib import nullcontext
from typing import Any, cast

from sqlalchemy.exc import OperationalError

from trading_core.recording import repository
from trading_core.recording import writer as writer_module
from trading_core.recording.enums import Env, RiskEventKind, Severity
from trading_core.recording.errors import MissingParentError, RecordConflictError
from trading_core.recording.rows import RiskEventRow
from trading_core.recording.spool import WriteCall, WriteItem, read
from trading_core.recording.writer import RecordingWriter
from trading_core.strategy.blocks import BlockReason, EntryBlocks

FAST = (0.05, 0.1, 0.2)


class FakeSession:
    def begin(self):
        return nullcontext()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _item(n):
    row = RiskEventRow(
        env=Env.DEMO,
        event_key=f"k{n}",
        kind=RiskEventKind.LIMIT_HIT,
        severity=Severity.INFO,
        message=str(n),
        occurred_ns=1_791_116_102_000_000_000 + n,
    )
    return WriteItem(WriteCall.RECORD_RISK_EVENT, row)


def _n(item):
    return int(item.row.message)


class Repository:
    """Stands in for ``record_risk_event``: writes, raises, or waits, per item number."""

    def __init__(self, monkeypatch, behave=None):
        self.written = []
        self.attempts = []
        self.behave = behave or (lambda n, attempt: None)
        monkeypatch.setitem(writer_module.CALLS, WriteCall.RECORD_RISK_EVENT, self)
        self.reported: list[RecordConflictError] = []
        monkeypatch.setattr(repository, "report_conflict", lambda session, error: self.reported.append(error))

    def __call__(self, session, row):
        n = int(row.message)
        attempt = sum(1 for a, _ in self.attempts if a == n)
        self.attempts.append((n, time.monotonic()))
        self.behave(n, attempt)
        self.written.append(n)


def _writer(tmp_path, blocks=None, **kw):
    options: dict[str, Any] = {"queue_max": 100, "max_backlog_s": 30.0, "retry_delays_s": FAST} | kw
    writer = RecordingWriter(
        lambda: cast(Any, FakeSession()), blocks or EntryBlocks(), deadletter=tmp_path / "dl.jsonl", **options
    )
    writer.start()
    return writer


def _until(condition, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def test_items_are_written_in_order_and_drain_reports_everything_written(monkeypatch, tmp_path):
    repo = Repository(monkeypatch)
    writer = _writer(tmp_path)
    for n in range(5):
        writer.put(_item(n))

    assert writer.drain(5.0) is True
    assert repo.written == [0, 1, 2, 3, 4]
    assert writer.backlog == 0


def test_a_transient_error_retries_the_same_item_in_order_after_each_delay_then_the_last_repeats(monkeypatch, tmp_path):
    def behave(n, attempt):
        if n == 0 and attempt < 4:
            raise OperationalError("SELECT 1", {}, ConnectionError("down"))

    repo = Repository(monkeypatch, behave)
    blocks = EntryBlocks()
    writer = _writer(tmp_path, blocks)
    writer.put(_item(0))
    writer.put(_item(1))

    _until(lambda: BlockReason.RECORDING_UNHEALTHY in blocks.active())  # the failed attempt blocks entries
    assert writer.drain(5.0) is True

    assert repo.written == [0, 1]  # item 1 waited for item 0
    times = [t for n, t in repo.attempts if n == 0]
    gaps = [b - a for a, b in itertools.pairwise(times)]
    assert len(gaps) == 4
    for gap, delay in zip(gaps, (*FAST, FAST[-1]), strict=True):
        assert gap >= delay * 0.9
    assert not (tmp_path / "dl.jsonl").exists()


def test_the_unhealthy_block_clears_once_the_queue_is_empty_and_the_last_write_succeeded(monkeypatch, tmp_path):
    def behave(n, attempt):
        if attempt == 0:
            raise OperationalError("SELECT 1", {}, ConnectionError("down"))

    Repository(monkeypatch, behave)
    blocks = EntryBlocks()
    writer = _writer(tmp_path, blocks)
    writer.put(_item(0))

    _until(lambda: BlockReason.RECORDING_UNHEALTHY in blocks.active())
    _until(lambda: blocks.active() == ())
    assert writer.last_ok
    writer.drain(1.0)


def test_a_write_stuck_longer_than_the_backlog_limit_blocks_entries_until_it_lands(monkeypatch, tmp_path):
    release = threading.Event()

    def behave(n, attempt):
        if n == 0:
            release.wait(5.0)  # Postgres hangs: no error, no answer

    repo = Repository(monkeypatch, behave)
    blocks = EntryBlocks()
    writer = _writer(tmp_path, blocks, max_backlog_s=0.2)
    writer.put(_item(0))
    writer.put(_item(1))

    _until(lambda: BlockReason.RECORDING_UNHEALTHY in blocks.active())
    release.set()
    _until(lambda: blocks.active() == ())
    assert repo.written == [0, 1]
    writer.drain(1.0)


def test_a_poison_item_is_dead_lettered_blocks_recording_failed_and_the_rest_still_lands(monkeypatch, tmp_path):
    def behave(n, attempt):
        if n == 1:
            raise MissingParentError("no order O-9 in this run")

    repo = Repository(monkeypatch, behave)
    blocks = EntryBlocks()
    writer = _writer(tmp_path, blocks)
    for n in range(3):
        writer.put(_item(n))

    assert writer.drain(5.0) is True
    assert repo.written == [0, 2]
    assert [_n(i) for i in read(tmp_path / "dl.jsonl")] == [1]
    assert BlockReason.RECORDING_FAILED in blocks.active()


def test_a_conflict_is_reported_dead_lettered_and_blocks_record_conflict(monkeypatch, tmp_path):
    conflict = RecordConflictError("risk_events", {"event_key": "k0"}, {"message": "a"}, {"message": "b"})

    def behave(n, attempt):
        if n == 0:
            raise conflict

    repo = Repository(monkeypatch, behave)
    blocks = EntryBlocks()
    writer = _writer(tmp_path, blocks)
    writer.put(_item(0))
    writer.put(_item(1))

    assert writer.drain(5.0) is True
    assert repo.reported == [conflict]
    assert [_n(i) for i in read(tmp_path / "dl.jsonl")] == [0]
    assert repo.written == [1]
    assert BlockReason.RECORD_CONFLICT in blocks.active()


def test_a_full_queue_overflows_counts_as_a_failed_write_and_spills_after_drain_in_order(monkeypatch, tmp_path):
    release = threading.Event()

    def behave(n, attempt):
        if n == 0:
            release.wait(5.0)

    Repository(monkeypatch, behave)
    blocks = EntryBlocks()
    writer = _writer(tmp_path, blocks, queue_max=1)
    writer.put(_item(0))
    _until(lambda: writer.backlog == 1 and writer.pending() and _n(writer.pending()[0]) == 0)  # in flight
    writer.put(_item(1))  # queued
    writer.put(_item(2))  # the queue is full: overflow

    assert writer.backlog == 3
    assert writer.last_ok is False
    assert BlockReason.RECORDING_UNHEALTHY in blocks.active()

    assert writer.drain(0.1) is False  # item 0 is still stuck
    spill = tmp_path / "run.jsonl"
    assert writer.spill(spill) == 3
    assert [_n(i) for i in read(spill)] == [0, 1, 2]
    release.set()


def test_a_drain_mid_retry_leaves_the_item_pending_for_the_spill(monkeypatch, tmp_path):
    def behave(n, attempt):
        raise OperationalError("SELECT 1", {}, ConnectionError("down"))

    Repository(monkeypatch, behave)
    writer = _writer(tmp_path, retry_delays_s=(10.0,))
    writer.put(_item(0))
    writer.put(_item(1))
    _until(lambda: writer.last_ok is False)

    assert writer.drain(0.2) is False
    assert [_n(i) for i in writer.pending()] == [0, 1]


def test_put_never_blocks_the_caller(monkeypatch, tmp_path):
    release = threading.Event()
    Repository(monkeypatch, lambda n, attempt: release.wait(5.0))
    writer = _writer(tmp_path, queue_max=1)

    started = time.monotonic()
    for n in range(50):
        writer.put(_item(n))
    assert time.monotonic() - started < 0.5
    release.set()
    writer.drain(5.0)

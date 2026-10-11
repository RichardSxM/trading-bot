"""Catalog writes are flushed to disk before and after the rename (spec 0007, the commit point)."""

import os

import pyarrow as pa
import pytest

from trading_core.catalog import sink
from trading_core.catalog.sink import HourSink, atomic_write
from trading_core.marketdata.errors import HourCountMismatch


@pytest.fixture
def calls(monkeypatch):
    """Record fsync and replace in order, naming each fsync by what its descriptor points at."""
    log = []
    names = {}
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def fake_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        names[fd] = os.fspath(path)
        return fd

    def fake_fsync(fd):
        log.append(("fsync", names[fd]))
        real_fsync(fd)

    def fake_replace(src, dst):
        log.append(("replace", os.fspath(src)))
        real_replace(src, dst)

    monkeypatch.setattr(sink.os, "open", fake_open)
    monkeypatch.setattr(sink.os, "fsync", fake_fsync)
    monkeypatch.setattr(sink.os, "replace", fake_replace)
    return log


def test_atomic_write_syncs_the_file_then_renames_then_syncs_the_folder(tmp_path, calls):
    path = tmp_path / "sub" / "done.json"
    atomic_write(path, lambda tmp: tmp.write_text("{}"))
    tmp = str(path) + ".tmp"
    assert calls == [("fsync", tmp), ("replace", tmp), ("fsync", str(path.parent))]
    assert path.read_text() == "{}"


def test_hour_commit_syncs_the_parquet_file_and_its_folder(tmp_path, calls):
    table = pa.table({"a": [1, 2, 3]})
    path = tmp_path / "data" / "h.parquet"
    hour = HourSink(path, table.schema, 0)
    hour.write(table)
    hour.commit(3)
    tmp = str(path) + ".tmp"
    assert calls == [("fsync", tmp), ("replace", tmp), ("fsync", str(path.parent))]


def test_a_count_mismatch_renames_nothing(tmp_path, calls):
    table = pa.table({"a": [1, 2, 3]})
    path = tmp_path / "h.parquet"
    hour = HourSink(path, table.schema, 0)
    hour.write(table)
    with pytest.raises(HourCountMismatch):
        hour.commit(4)
    assert calls == []
    assert not path.exists()

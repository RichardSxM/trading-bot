"""read_chunks: a torn tail ends the file quietly, damage before the end fails the pass (AC-2)."""

import pyarrow as pa
import pytest

from recorder.compactor.convert import read_chunks
from trading_core.marketdata.errors import FeatherUnreadable


def _stream(path, batches=100, rows=10):
    """A feather stream of ``batches`` batches; returns the byte offset where each batch starts."""
    table = pa.table({"a": list(range(batches * rows))})
    starts = []
    with pa.OSFile(str(path), "wb") as sink, pa.ipc.new_stream(sink, table.schema) as writer:
        for batch in table.to_batches(max_chunksize=rows):
            starts.append(sink.tell())
            writer.write_batch(batch)
    return starts


def _rows(path):
    return [v for chunk in read_chunks(path) for v in chunk.column("a").to_pylist()]


def test_every_torn_tail_ends_at_the_last_complete_batch(tmp_path):
    path = tmp_path / "f.feather"
    _stream(path)
    data = path.read_bytes()
    for cut in range(len(data) - 1, len(data) - 400, -3):
        path.write_bytes(data[:cut])
        rows = _rows(path)
        assert rows == list(range(len(rows)))


def test_damage_before_the_end_raises_instead_of_dropping_the_rest(tmp_path):
    path = tmp_path / "f.feather"
    starts = _stream(path)
    data = bytearray(path.read_bytes())
    middle = starts[len(starts) // 2]
    data[middle : middle + 8] = b"\xff" * 8
    path.write_bytes(bytes(data))
    with pytest.raises(FeatherUnreadable) as caught:
        _rows(path)
    assert caught.value.position < caught.value.size

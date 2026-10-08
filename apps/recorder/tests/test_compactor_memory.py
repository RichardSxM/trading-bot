"""covers: spec 0007 AC-9 (a 2 GB feather converts inside the 512 MB limit, read once).

Slow (several minutes, 2 GB of disk), so it runs only with ``KFS_SLOW_TESTS=1``:
``docker compose run --rm --no-deps -T -e KFS_SLOW_TESTS=1 recorder pytest -q tests/test_compactor_memory.py``
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pytest
from feed_fixture import H0, HOUR_NS, SECOND_NS, make_feed, write_run

pytestmark = pytest.mark.skipif(os.environ.get("KFS_SLOW_TESTS") != "1", reason="slow; set KFS_SLOW_TESTS=1")

LIMIT_MB = 512
TARGET_BYTES = 2 * 1024**3


def build(root: str) -> int:
    """Write a 2 GB dead run and a small current run under ``root``. Returns the big run's rows.

    Run in its own process, so the generator's memory is gone before the compactor starts (the
    test container shares one memory limit).
    """
    live = Path(root) / "live"
    block_ns = 60 * SECOND_NS
    feed = make_feed(H0 + SECOND_NS, H0 + block_ns, step_ms=(1, 3))
    [path] = write_run(live, "a-dead", feed.messages, rotate_at=[])
    del feed
    block = pa.ipc.open_stream(pa.OSFile(str(path), "rb")).read_all()
    # Copies of the block, each a minute later, in tiny batches like the recorder writes.
    rows = 0
    with pa.OSFile(str(path), "wb") as sink:
        writer = pa.ipc.new_stream(sink, block.schema)
        copy = 0
        while sink.tell() < TARGET_BYTES:
            shift = pa.scalar(copy * block_ns, pa.uint64())
            shifted = block
            for name in ("ts_event", "ts_init"):
                i = shifted.schema.get_field_index(name)
                shifted = shifted.set_column(i, block.schema.field(i), pc.add(shifted.column(name), shift))
            for batch in shifted.to_batches(max_chunksize=2):
                writer.write_batch(batch)
            rows += shifted.num_rows
            copy += 1
        writer.close()
    # A newer run, so the big one is a dead run and closed.
    later = make_feed(H0 + 30 * HOUR_NS, H0 + 30 * HOUR_NS + 60 * SECOND_NS, seed=3)
    write_run(live, "z-current", later.messages, rotate_at=[])
    return rows


def _python(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).parent,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def test_a_two_gigabyte_file_converts_within_the_memory_limit(tmp_path):
    rows = int(_python(f"from test_compactor_memory import build; print(build({str(tmp_path)!r}))").split()[-1])
    out = _python(
        f"""
        import resource, time
        from pathlib import Path
        from recorder.compactor.main import Compactor
        from recorder.compactor.store import CatalogStore
        store = CatalogStore(Path({str(tmp_path)!r}))
        Compactor(store, grace_ns=0, floor_bytes=0).run_pass(time.time_ns() + 10**15)
        hours = store.load_done().hours
        print(sum(v["rows"] - v["snapshot_rows"] for h in hours.values() for v in h.values() if "snapshot_rows" in v))
        print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
        """
    )
    converted, peak_mb = (int(v) for v in out.strip().splitlines()[-2:])
    assert converted == rows
    assert peak_mb < LIMIT_MB, f"peak RSS {peak_mb} MB"

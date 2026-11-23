import asyncio
import logging

import pytest

from trader.load_probe import cgroup_mib, ready_callbacks, rss_mib, watch_load


def test_rss_reads_vmrss_in_mib(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t  2097152 kB\nThreads:\t9\n")
    assert rss_mib(status) == 2048


def test_rss_is_none_when_unreadable(tmp_path):
    assert rss_mib(tmp_path / "missing") is None


def test_cgroup_reads_bytes_in_mib(tmp_path):
    current = tmp_path / "memory.current"
    current.write_text("1610612736\n")
    assert cgroup_mib(current) == 1536


def test_cgroup_is_none_when_garbled(tmp_path):
    current = tmp_path / "memory.current"
    current.write_text("max\n")
    assert cgroup_mib(current) is None


def test_ready_callbacks_counts_the_loop_backlog():
    loop = asyncio.new_event_loop()
    try:
        for _ in range(3):
            loop.call_soon(lambda: None)
        assert ready_callbacks(loop) == 3
    finally:
        loop.close()


def test_watch_load_logs_and_survives_a_queue_read_error(tmp_path, caplog):
    calls = iter([{"data_q": 7}, RuntimeError("engine gone")])

    def queue_sizes():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    async def run():
        task = asyncio.create_task(
            watch_load(
                asyncio.get_running_loop(),
                queue_sizes,
                interval_s=0.01,
                status=tmp_path / "none",
                current=tmp_path / "none",
            )
        )
        await asyncio.sleep(0.015)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with caplog.at_level(logging.INFO, logger="trader.load_probe"):
        asyncio.run(run())
    probes = [r for r in caplog.records if r.fields["code"] == "load_probe"]
    assert probes[0].fields["data_q"] == 7
    assert probes[0].fields["rss_mib"] is None
    assert any(r.fields["code"] == "load_probe_failed" for r in caplog.records)
    assert len(probes) == 2

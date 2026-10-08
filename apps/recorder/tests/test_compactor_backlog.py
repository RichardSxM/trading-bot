"""covers: spec 0007 AC-9 (the laptop backlog: `--offline` conversion and the bridge gap), AC-5 (a day wholly
inside a gap closes, and a gap longer than a day counts on every day it covers)."""

import json
import logging
import time

import pytest
from feed_fixture import H0, HOUR_NS, INSTRUMENT, SECOND_NS, make_feed, write_run

from recorder.compactor import convert
from recorder.compactor.bridge import bridge
from recorder.compactor.days import read_gaps
from recorder.compactor.main import Compactor, run_offline
from recorder.compactor.store import DELTAS_TABLE, CatalogStore, stream_key
from trading_core.marketdata.errors import BridgeRefused
from trading_core.marketdata.gaps import Gap
from trading_core.marketdata.hours import DAY_NS, day_of

MINUTE_NS = 60 * SECOND_NS
DELTAS = stream_key(DELTAS_TABLE, INSTRUMENT)
DAY = day_of(H0)  # 2026-10-08 00:00 UTC
SPARSE = (20_000, 30_000)  # a message every 20 to 30 s keeps day long feeds small


def _later():
    return max(time.time_ns(), DAY + 30 * DAY_NS)


def _codes(caplog, code):
    return [r.fields for r in caplog.records if getattr(r, "fields", {}).get("code") == code]


def _offline(store, **kwargs):
    return Compactor(store, grace_ns=0, floor_bytes=0, offline=True, **kwargs)


def _two_runs(store):
    """Two dead runs and no current one, as on the laptop: the newest run is not being written."""
    a = make_feed(H0 + 7 * SECOND_NS, H0 + 30 * MINUTE_NS).messages
    b = make_feed(H0 + HOUR_NS + 10 * MINUTE_NS, H0 + 2 * HOUR_NS + 20 * MINUTE_NS, seed=9).messages
    write_run(store.live, "a-dead", a, rotate_at=[])
    write_run(store.live, "b-dead", b, rotate_at=[H0 + 2 * HOUR_NS])
    return a, b


def test_offline_finishes_the_newest_run_too(tmp_path):
    online, offline = CatalogStore(tmp_path / "online"), CatalogStore(tmp_path / "offline")
    _two_runs(online)
    _two_runs(offline)

    Compactor(online, grace_ns=0, floor_bytes=0).run_pass(_later())
    _offline(offline).run_pass(_later())

    # Online, run b counts as the current run, so its newest file is never read.
    assert H0 + 2 * HOUR_NS not in online.load_done().hours.get(DELTAS, {})
    assert sorted(offline.load_done().hours[DELTAS]) == [H0, H0 + HOUR_NS, H0 + 2 * HOUR_NS]
    assert not list(offline.live.rglob("*.feather"))


def test_run_offline_exits_0_when_everything_converted(tmp_path, caplog):
    store = CatalogStore(tmp_path)
    _two_runs(store)
    with caplog.at_level(logging.INFO):
        assert run_offline(_offline(store), store, _later) == 0
    [done] = _codes(caplog, "compactor_offline_done")
    assert done["ok"] is True
    assert done["feather_left"] == 0


def test_run_offline_exits_1_on_a_mismatch_and_keeps_the_feather(tmp_path, monkeypatch, caplog):
    store = CatalogStore(tmp_path)
    _two_runs(store)
    real_commit = convert.HourSink.commit
    monkeypatch.setattr(convert.HourSink, "commit", lambda self, expected: real_commit(self, expected + 1))
    with caplog.at_level(logging.INFO):
        assert run_offline(_offline(store), store, _later) == 1
    [done] = _codes(caplog, "compactor_offline_done")
    assert done["ok"] is False
    assert "compactor_pass_failed" in done["failed_codes"]
    assert list(store.live.rglob("*.feather"))


def _backlog_and_vps(tmp_path):
    """The backlog ends on day D at 20:30; the VPS catalog starts on D + 2 at 03:10."""
    backlog, vps = CatalogStore(tmp_path / "catalog"), CatalogStore(tmp_path / "catalog-vps")
    a = make_feed(DAY + 18 * HOUR_NS + 5 * SECOND_NS, DAY + 20 * HOUR_NS + 30 * MINUTE_NS, step_ms=SPARSE)
    v = make_feed(DAY + 2 * DAY_NS + 3 * HOUR_NS + 10 * MINUTE_NS, DAY + 2 * DAY_NS + 4 * HOUR_NS, seed=5)
    write_run(backlog.live, "a-dead", a.messages, rotate_at=[])
    write_run(vps.live, "v-dead", v.messages, rotate_at=[])
    assert run_offline(_offline(backlog, silence_ns=HOUR_NS), backlog, _later) == 0
    assert run_offline(_offline(vps), vps, _later) == 0
    return backlog, vps, a.messages[-1][0].ts_init, v.messages[0][0].ts_init


def test_the_bridge_gap_runs_from_the_backlog_to_the_vps_and_closes_the_days_between(tmp_path):
    backlog, vps, last_ns, first_ns = _backlog_and_vps(tmp_path)
    assert not backlog.load_done().days  # day D waits for the hours after 20:30

    gaps = bridge(backlog, vps.root)

    expected = Gap(last_ns, INSTRUMENT, "recorder_down", first_ns)
    assert gaps == [expected]
    assert read_gaps(backlog, "2026-10-08") == [expected]
    done = backlog.load_done()
    day_one = DAY + DAY_NS
    assert done.empty[INSTRUMENT] == set(range(DAY + 21 * HOUR_NS, DAY + 2 * DAY_NS + 3 * HOUR_NS, HOUR_NS))
    # D and D + 1 (wholly inside the gap) close; D + 2 belongs to the VPS and stays open here.
    assert done.days == {"2026-10-08", "2026-10-09"}
    one = json.loads(backlog.manifest_file("2026-10-08").read_text())
    two = json.loads(backlog.manifest_file("2026-10-09").read_text())
    assert one["gaps"] == [
        {"instrument_id": INSTRUMENT, "kind": "recorder_down", "seconds": pytest.approx((day_one - last_ns) / 1e9)}
    ]
    assert two["gaps"] == [{"instrument_id": INSTRUMENT, "kind": "recorder_down", "seconds": 86_400.0}]
    assert two["streams"] == []
    assert not backlog.manifest_file("2026-10-10").exists()


def test_a_bridge_rerun_changes_nothing(tmp_path):
    backlog, vps, _, _ = _backlog_and_vps(tmp_path)
    bridge(backlog, vps.root)
    files = {p: p.read_bytes() for p in backlog.root.rglob("*") if p.is_file()}

    bridge(backlog, vps.root)

    assert {p: p.read_bytes() for p in backlog.root.rglob("*") if p.is_file()} == files


def test_an_instrument_on_one_side_only_gets_no_gap_and_a_warning(tmp_path, caplog):
    backlog, vps, _, _ = _backlog_and_vps(tmp_path)
    (vps.root / "data" / DELTAS_TABLE / "ETHUSDT-PERP.BINANCE").mkdir()
    with caplog.at_level(logging.WARNING):
        gaps = bridge(backlog, vps.root)
    assert [g.instrument_id for g in gaps] == [INSTRUMENT]
    [missing] = _codes(caplog, "bridge_instrument_missing")
    assert (missing["instrument_id"], missing["absent_from"]) == ("ETHUSDT-PERP.BINANCE", "backlog")


def test_the_bridge_refuses_while_feather_is_left(tmp_path):
    backlog, vps, _, _ = _backlog_and_vps(tmp_path)
    write_run(backlog.live, "c-dead", make_feed(DAY + 22 * HOUR_NS, DAY + 22 * HOUR_NS + MINUTE_NS).messages, [])
    before = backlog.load_done().to_json()
    with pytest.raises(BridgeRefused, match="feather"):
        bridge(backlog, vps.root)
    assert backlog.load_done().to_json() == before
    assert not read_gaps(backlog, "2026-10-08")


def test_the_bridge_refuses_without_a_vps_delta_row(tmp_path):
    backlog, _, _, _ = _backlog_and_vps(tmp_path)
    empty = tmp_path / "empty"
    (empty / "data" / DELTAS_TABLE / INSTRUMENT).mkdir(parents=True)
    with pytest.raises(BridgeRefused, match="no VPS delta row"):
        bridge(backlog, empty)
    assert not read_gaps(backlog, "2026-10-08")


def test_a_gap_longer_than_a_day_counts_on_every_day_it_covers(tmp_path):
    store = CatalogStore(tmp_path)
    a = make_feed(DAY + 22 * HOUR_NS + 5 * SECOND_NS, DAY + 22 * HOUR_NS + 50 * MINUTE_NS, step_ms=SPARSE).messages
    b = make_feed(
        DAY + 2 * DAY_NS + HOUR_NS + 20 * MINUTE_NS, DAY + 3 * DAY_NS + 30 * MINUTE_NS, seed=9, step_ms=SPARSE
    )
    write_run(store.live, "a-dead", a, rotate_at=[])
    write_run(store.live, "b-dead", b.messages, rotate_at=[])
    later = make_feed(DAY + 3 * DAY_NS + 5 * HOUR_NS, DAY + 3 * DAY_NS + 5 * HOUR_NS + MINUTE_NS, seed=3)
    write_run(store.live, "z-current", later.messages, rotate_at=[])

    Compactor(store, grace_ns=0, floor_bytes=0, silence_ns=HOUR_NS).run_pass(_later())

    gap = Gap(a[-1][0].ts_init, INSTRUMENT, "recorder_down", b.messages[0][0].ts_init)
    assert read_gaps(store, "2026-10-08") == [gap]
    assert {"2026-10-08", "2026-10-09", "2026-10-10"} <= store.load_done().days
    seconds = {
        day: json.loads(store.manifest_file(day).read_text())["gaps"][0]["seconds"]
        for day in ("2026-10-08", "2026-10-09", "2026-10-10")
    }
    assert seconds["2026-10-08"] == pytest.approx((DAY + DAY_NS - gap.start_ns) / 1e9)
    assert seconds["2026-10-09"] == 86_400.0
    assert seconds["2026-10-10"] == pytest.approx((gap.end_ns - (DAY + 2 * DAY_NS)) / 1e9)

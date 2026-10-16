"""covers: spec 0013 AC-8 (a live fill is one silent push naming its fills row, looked up every 2 s for up to
10 s; a demo fill pushes nothing)."""

import asyncio
import json

from trading_core.alerts.model import Tier

from .test_docker_logs import FakeDocker, stamp
from .test_service_sources import make, settle

ROW = {
    "instrument_id": "PF_XBTUSD.KRAKEN",
    "side": "buy",
    "quantity": "0.001",
    "price": "67412.5",
    "fee": "0.02",
    "fee_currency": "USD",
    "base_currency": "BTC",
    "lot_size": "0.0001",
    "tick_size": "0.5",
}


def fill_line(trade_id: str = "T-1") -> str:
    body = {"level": "INFO", "code": "fill_seen", "trade_id": trade_id, "msg": "fill seen"}
    return f"{stamp(-1)} " + json.dumps(body)


class Sleeps:
    """Records each sleep; ``store`` gets the row after ``row_after`` sleeps."""

    def __init__(self, store, row_after: int | None) -> None:
        self.store, self.row_after = store, row_after
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.row_after is not None and len(self.slept) >= self.row_after:
            self.store.fills["T-1"] = ROW


def run_fill(tmp_path, *, env="live", row_after=None):
    async def run():
        docker = FakeDocker()
        sleeps = Sleeps(None, row_after)
        alerter, _, deliverer, store, _ = make(tmp_path, docker, env=env, sleep=sleeps)
        sleeps.store = store
        if row_after == 0:
            store.fills["T-1"] = ROW
        docker.lines["t1"] = [fill_line()]
        await alerter.read_logs()
        await settle(alerter)
        return deliverer, store, sleeps

    return asyncio.run(run())


def test_a_live_fill_already_stored_pushes_its_row_at_once(tmp_path):
    deliverer, store, sleeps = run_fill(tmp_path, row_after=0)

    (push,) = deliverer.sent
    assert (push.tier, push.episode.fingerprint) == (Tier.INFO, "fill:T-1")
    assert push.message == "Bought 0.0010 BTC at 67,412.5, fee 0.02 USD"
    assert (store.fill_reads, sleeps.slept) == (["T-1"], [])


def test_a_live_fill_stored_late_is_found_on_a_later_read(tmp_path):
    deliverer, store, sleeps = run_fill(tmp_path, row_after=2)

    (push,) = deliverer.sent
    assert push.message.startswith("Bought 0.0010 BTC")
    assert (len(store.fill_reads), sleeps.slept) == (3, [2.0, 2.0])


def test_a_fill_never_stored_pushes_its_trade_id_after_ten_seconds(tmp_path):
    deliverer, store, sleeps = run_fill(tmp_path)

    (push,) = deliverer.sent
    assert push.message == "fill T-1"
    assert (len(store.fill_reads), sum(sleeps.slept)) == (6, 10.0)


def test_a_demo_fill_is_never_looked_up_or_pushed(tmp_path):
    deliverer, store, _ = run_fill(tmp_path, env="demo", row_after=0)

    assert (deliverer.sent, store.fill_reads) == ([], [])

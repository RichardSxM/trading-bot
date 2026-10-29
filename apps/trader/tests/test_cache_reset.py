"""covers: spec 0023 AC-8, AC-26 (``python -m trader.cache reset`` deletes only ``trader-<trader id>:*`` and the
untrusted cache set, never our other ``{env}:*`` keys, so the halt stays latched, and refuses while the trader's
heartbeat snapshot is fresh)."""

import fnmatch

import pytest

from trader.cache import TraderRunning, heartbeat_alive, main, reset
from trading_core.snapshot.envelope import encode

SECOND = 1_000_000_000

TRADER = "TRADER-LIVE"


class FakeRedis:
    def __init__(self, *keys):
        self.keys, self.values = set(keys), {}

    def get(self, name):
        return self.values.get(name)

    def scan_iter(self, match, count):
        return iter([k for k in sorted(self.keys) if fnmatch.fnmatchcase(k, match)])

    def unlink(self, *names):
        gone = self.keys & set(names)
        self.keys -= gone
        return len(gone)


def test_reset_deletes_the_trader_cache_and_the_untrusted_set_only():
    redis = FakeRedis(
        f"trader-{TRADER}:orders:O-1",
        f"trader-{TRADER}:positions:P-1",
        "live:reconcile:cache_untrusted",
        "live:trader:halt",
        "live:trader:pause",
        "live:control",
        f"trader-{TRADER}X:orders:O-1",
    )

    deleted = reset(redis, TRADER, "live", alive=lambda: False)

    assert deleted == 3
    assert redis.keys == {"live:trader:halt", "live:trader:pause", "live:control", f"trader-{TRADER}X:orders:O-1"}


def test_reset_refuses_while_the_trader_is_alive_and_deletes_nothing():
    redis = FakeRedis(f"trader-{TRADER}:orders:O-1", "live:reconcile:cache_untrusted")

    with pytest.raises(TraderRunning):
        reset(redis, TRADER, "live", alive=lambda: True)

    assert len(redis.keys) == 2


def test_the_guard_reads_the_trader_heartbeat_snapshot():
    redis = FakeRedis()
    now = 100 * SECOND
    assert not heartbeat_alive(redis, "live", lambda: now)  # no heartbeat: the trader is stopped

    redis.values["live:snapshot:heartbeat"] = encode(now - 5 * SECOND, {})
    assert heartbeat_alive(redis, "live", lambda: now)

    redis.values["live:snapshot:heartbeat"] = encode(now - 31 * SECOND, {})
    assert not heartbeat_alive(redis, "live", lambda: now)


def test_a_heartbeat_that_does_not_parse_counts_as_alive():
    redis = FakeRedis()
    redis.values["live:snapshot:heartbeat"] = "not json"

    assert heartbeat_alive(redis, "live")


def test_a_wrong_command_exits_2(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["purge"])

    assert exit_info.value.code == 2
    assert "usage" in capsys.readouterr().err

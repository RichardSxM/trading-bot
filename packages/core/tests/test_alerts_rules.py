"""covers: spec 0013 *Rule table* for log lines (AC-3, AC-4, AC-7): each row's match, tier, and
fingerprint, and the precedence of the first match.

Pure: no clock, no I/O.
"""

from datetime import UTC, datetime

import pytest

from trading_core.alerts.model import Observation, Tier
from trading_core.alerts.rules import BY_ID, first_match, partner_of

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)


def line(service, code=None, level="ERROR", text=None, **fields):
    return Observation(
        source=f"log:{service}",
        service=service,
        at=NOW,
        code=code,
        level=None if text else level,
        fields={**fields, **({"code": code} if code else {})},
        text=text,
    )


ROWS = [
    # (observation, rule, fingerprint)
    (line("trader", "loss_limit_halt", "CRITICAL", halt_id="h1"), "loss_limit_halt", "halt:h1"),
    (line("trader", "halt", "CRITICAL", event_key="halt:h1"), "risk_critical", "halt:h1"),
    (
        line("trader", "order_rejected", "CRITICAL", event_key="stop_not_accepted:S1"),
        "risk_critical",
        "stop_not_accepted:S1",
    ),
    (line("trader", "liq_near", "CRITICAL", event_key="liq_near:7"), "risk_critical", "liq_near:7"),
    (line("trader", "halt", "WARNING", event_key="halt:h2"), "risk_halt_manual", "halt:h2"),
    (line("trader", "positions_abandoned", "CRITICAL"), "trader_critical_codes", "positions_abandoned"),
    (line("trader", "kill_switch_task_crashed", "CRITICAL"), "trader_critical_codes", "kill_switch_task_crashed"),
    (line("trader", "halt_key_invalid", "CRITICAL"), "trader_critical_codes", "halt_key_invalid"),
    (line("trader", "record_conflict", "ERROR", table="fills"), "trader_critical_codes", "record_conflict"),
    (line("recorder", "catalog_disk_full", "CRITICAL"), "catalog_disk_full", "catalog_disk_full"),
    (line("recorder", "recorder_write_failed", "CRITICAL"), "recorder_write_failed", "recorder_write_failed"),
    (line("trader", "kraken_auth_failed", "CRITICAL"), "key_and_secret_codes", "kraken_auth_failed:trader"),
    (line("dashboard", "secret_unavailable", "CRITICAL"), "key_and_secret_codes", "secret_unavailable:dashboard"),
    (line("trader", "kraken_key_check_crashed"), "key_and_secret_codes", "kraken_key_check_crashed:trader"),
    (line("backup", "backup_failed", step="restic_backup"), "backup_failed", "backup_failed:restic_backup"),
    (line("backup", "backup_failed", step="catalog"), "backup_failed", "backup_failed:catalog"),
    (line("backup", "restic_repo_missing"), "backup_failed", "backup_failed:repo"),
    (line("recorder", "context_feed_down", "WARNING"), "context_feed_down", "context_feed_down:recorder"),
    (line("trader", "context_feed_down", "WARNING"), "context_feed_down", "context_feed_down:trader"),
    (
        line("compactor", "book_gap", "WARNING", kind="recorder_down", duration_s=61.5),
        "book_gap_long",
        "book_gap_long:recorder_down",
    ),
    (line("recorder", "book_sequence_hole"), "book_sequence_hole", "book_sequence_hole"),
    (line("compactor", "catalog_retention_blocked"), "recorder_codes", "catalog_retention_blocked"),
    (line("recorder", "recorder_flush_slow", "WARNING"), "recorder_codes", "recorder_flush_slow"),
    (line("trader", "kraken_key_rotation_due", "WARNING"), "kraken_key_rotation_due", "kraken_key_rotation_due"),
    (line("trader", "snapshot_write_failed", "WARNING"), "snapshot_write_failed", "snapshot_write_failed"),
    (
        line("trader", text="2026-10-16T03:00:00Z [ERROR] TRADER-001.ExecEngine: boom"),
        "engine_error",
        "engine_error:trader",
    ),
    (line("recorder", text="[ERROR] RECORDER-001.DataEngine: boom"), "engine_error", "engine_error:recorder"),
    (line("trader", "stale_feed", "WARNING", event_key="stale_feed:r:1"), "stale_feed", "stale_feed"),
    (line("trader", "limit_hit", "WARNING", event_key="trade_cap:2026-10-16"), "trade_cap", "trade_cap:2026-10-16"),
]


@pytest.mark.parametrize(("obs", "rule_id", "fingerprint"), ROWS, ids=[r[1] + ":" + r[2] for r in ROWS])
def test_each_row_matches_and_fingerprints(obs, rule_id, fingerprint):
    rule = first_match(obs)

    assert rule is not None
    assert rule.id == rule_id
    assert rule.key is not None
    assert rule.key(obs) == fingerprint


@pytest.mark.parametrize(
    "obs",
    [
        line("compactor", "book_gap", "WARNING", kind="recorder_down", duration_s=60),  # not over 60 s
        line("trader", "halt", "INFO", event_key="halt:h3"),  # a halt at neither level
        line("trader", "order_rejected", "ERROR", event_key="stop_not_accepted:S2"),  # not CRITICAL
        line("dashboard", "positions_abandoned", "CRITICAL"),  # a trader code from another service
        line("dashboard", text="[ERROR] not an engine"),  # engine_error is trader and recorder only
        line("trader", text="Traceback (most recent call last):"),  # traceback lines are ignored
        line("trader", "entries_blocked", "INFO", reason="paused"),
        line("recorder", "catalog_disk_ok", "INFO"),  # a partner code opens nothing
    ],
)
def test_lines_no_rule_takes(obs):
    assert first_match(obs) is None


def test_a_loss_limit_halt_and_its_halt_event_share_a_fingerprint():
    halt_line = line("trader", "loss_limit_halt", "CRITICAL", halt_id="h9")
    event = line("trader", "halt", "CRITICAL", event_key="halt:h9")

    first, second = first_match(halt_line), first_match(event)

    assert first is not None
    assert second is not None
    assert first.key is not None
    assert second.key is not None
    assert first.key(halt_line) == second.key(event) == "halt:h9"


def test_the_first_match_wins():
    # A CRITICAL trader line with an event_key and a critical code: risk_critical comes first.
    obs = line("trader", "positions_abandoned", "CRITICAL", event_key="abandoned:P1")

    rule = first_match(obs)

    assert rule is not None
    assert rule.id == "risk_critical"


def test_partner_codes_name_their_pair_rule():
    assert partner_of(line("recorder", "catalog_disk_ok", "INFO")) is BY_ID["catalog_disk_full"]
    assert partner_of(line("trader", "context_feed_up", "INFO")) is BY_ID["context_feed_down"]
    assert partner_of(line("trader", "context_feed_down", "WARNING")) is None


def test_tiers_follow_the_table():
    critical = {"loss_limit_halt", "risk_critical", "trader_critical_codes", "catalog_disk_full", "service_down"}
    info = {"risk_halt_manual", "trade_cap"}

    assert {r for r in critical if BY_ID[r].tier is Tier.CRITICAL} == critical
    assert {r for r in info if BY_ID[r].tier is Tier.INFO} == info
    assert BY_ID["context_feed_down"].min_open_s == 300
    assert BY_ID["postgres_unreachable"].min_open_s == BY_ID["redis_unreachable"].min_open_s == 60
    assert BY_ID["service_down"].min_open_s == BY_ID["container_unhealthy"].min_open_s == 120

"""Every alert an earlier spec promised, and the rule that keeps it (spec 0013 AC-19, rationale *Promise inventory*).

A promise made by a log line carries a sample of that line, so the coverage test can prove the
rule table still catches it. A promise kept by a condition check names its rule only. The
healthchecks.io promises (`backup-verified`, `restore-drill`) are kept outside the rule table.
Pure data.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from trading_core.alerts.model import Observation
from trading_core.alerts.rules import DEPLOY, HOST


@dataclass(frozen=True)
class Promise:
    """One promised alert: the spec that made it, what it names, and the rule that keeps it.

    ``service``, ``level``, ``code``, ``fields``, and ``text`` describe a sample line when the
    promise is kept by a log rule; ``service`` is None for a promise kept by a condition check.
    """

    spec: str
    what: str
    rule: str
    service: str | None = None
    level: str | None = None
    code: str | None = None
    fields: Mapping[str, object] = field(default_factory=dict)
    text: str | None = None

    def sample(self, at: datetime) -> Observation | None:
        """The sample line as an ``Observation`` at ``at``; None for a condition promise."""
        if self.service is None:
            return None
        return Observation(
            source=self.service if self.service in (HOST, DEPLOY) else f"log:{self.service}",
            service=self.service,
            at=at,
            code=self.code,
            level=self.level,
            fields={**self.fields, **({"code": self.code} if self.code else {})},
            text=self.text,
        )


def _risk(spec: str, event_key: str, code: str = "order_rejected") -> Promise:
    """A critical trader risk event; ``code`` is the risk event kind it is logged under."""
    return Promise(spec, event_key.split(":")[0], "risk_critical", "trader", "CRITICAL", code, {"event_key": event_key})


def _line(spec: str, code: str, rule: str, service: str, level: str = "ERROR", **fields: object) -> Promise:
    return Promise(spec, code, rule, service, level, code, fields)


PROMISES: Final[tuple[Promise, ...]] = (
    Promise("0001", "trader heartbeat", "trader_down"),
    _risk("0001", "stop_not_accepted:O-1"),
    Promise("0001", "clock not running", "service_down"),
    Promise("0001", "clock unhealthy", "container_unhealthy"),
    _line("0003", "kraken_auth_failed", "key_and_secret_codes", "trader", "CRITICAL"),
    _line("0003", "kraken_key_rotation_due", "kraken_key_rotation_due", "trader", "WARNING"),
    _line("0003", "kraken_key_check_crashed", "key_and_secret_codes", "trader"),
    _line("0003", "secret_unavailable", "key_and_secret_codes", "dashboard", "CRITICAL", secret="x"),
    Promise("0004", "login_failed burst", "login_failed_burst"),
    _line("0004", "snapshot_write_failed", "snapshot_write_failed", "trader", "WARNING"),
    Promise(
        "0005",
        "autoheal_gave_up",
        "autoheal_gave_up",
        HOST,
        "CRITICAL",
        fields={"msg": "autoheal_gave_up", "container": "kfs-trader-1"},
    ),
    _line("0005", "backup_failed", "backup_failed", "backup", step="restic_backup"),
    Promise("0005", "backup unhealthy", "container_unhealthy"),
    Promise("0005", "rollback_failed", "rollback_failed", DEPLOY, fields={"sha": "abc", "outcome": "rollback_failed"}),
    Promise("0005", "disk under 5 GB", "disk_low"),
    _risk("0006", "stop_not_accepted:O-2"),
    _risk("0006", "exit_failed:P-1"),
    _risk("0006", "abandoned:P-1"),
    _line("0006", "record_conflict", "trader_critical_codes", "trader", table="fills"),
    _line("0006", "positions_abandoned", "trader_critical_codes", "trader", "CRITICAL"),
    Promise("0006", "entry block over an hour", "entry_block"),
    _line("0007", "book_gap", "book_gap_long", "compactor", "WARNING", kind="recorder_down", duration_s=61.0),
    _line("0007", "catalog_disk_full", "catalog_disk_full", "recorder", "CRITICAL"),
    _line("0007", "catalog_retention_blocked", "recorder_codes", "compactor"),
    _line("0007", "catalog_late_rows", "recorder_codes", "compactor", "WARNING"),
    _line("0007", "recorder_queue_full", "recorder_codes", "recorder", "WARNING"),
    _line("0007", "recorder_stop_queue_full", "recorder_codes", "recorder"),
    _line("0007", "compactor_pass_failed", "recorder_codes", "compactor"),
    Promise(
        "0007", "backup_failed step=catalog", "backup_failed", "backup", "ERROR", "backup_failed", {"step": "catalog"}
    ),
    _line("0007", "book_sequence_hole", "book_sequence_hole", "recorder"),
    _line("0009", "context_feed_down", "context_feed_down", "recorder", "WARNING"),
    _line(
        "0009",
        "context_hour_missing",
        "context_hour_missing",
        "compactor",
        "WARNING",
        hour="2026-10-16T03:00Z",
        table="mark_price_update",
    ),
    _risk("0010", "halt:H-1", "halt"),
    _risk("0010", "flatten_timeout:F-1", "halt"),
    _risk("0010", "halt_key_write_failed:H-1", "halt"),
    _line("0012", "recorder_write_failed", "recorder_write_failed", "recorder", "CRITICAL"),
    _line("0012", "recorder_writer_busy", "recorder_codes", "recorder", "WARNING"),
    _line("0012", "recorder_flush_slow", "recorder_codes", "recorder", "WARNING"),
    Promise("review 2026-10-11", "recorder engine ERROR line", "engine_error", "recorder", text="[ERROR] RECORDER: x"),
    Promise("scope", "fills", "fill"),
)

PENDING: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fill": "live fill pushes, Build plan 4.1",
    }
)
"""Promised rules not in the table yet, with what adds them; each entry leaves when its rule lands."""

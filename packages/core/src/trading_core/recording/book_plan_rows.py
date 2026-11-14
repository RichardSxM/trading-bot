"""The ``book_plans`` and ``book_plan_coins`` row values and their reason enum (spec 0033 AC-5).

Kept out of ``rows.py`` and ``enums.py`` on purpose: both sit in every strategy's code closure, and spec 0033
AC-4 forbids changing a registered version's hash. No ``trading_core.node`` module imports this file. The
rows still build on ``rows._Row``, so they get the same checks (``*_at`` from ``*_ns``, no floats).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from trading_core.recording.enums import Env
from trading_core.recording.rows import _Row


class BookPlanReason(StrEnum):
    """Why a book strategy rebalanced (``book_plans.reason``): the trader's ``last_reason`` values."""

    START = "start"
    RESUME = "resume"
    DAILY = "daily"
    WEEKLY = "weekly"


@dataclass(frozen=True, kw_only=True)
class BookPlanRow(_Row):
    """One rebalance of a book strategy (``book_plans``); vol fields are null when not vol sized."""

    id: UUID = field(default_factory=uuid.uuid7)
    env: Env
    run_id: UUID
    plan_day: date
    reason: BookPlanReason
    rebalanced_ns: int
    rebalanced_at: datetime = field(init=False)
    equity: Decimal
    n: int
    gross: Decimal
    unit_vol: Decimal | None = None
    book_gross: Decimal | None = None
    book_vol_target: Decimal | None = None
    rebalance_band: Decimal | None = None
    gross_cap: Decimal


@dataclass(frozen=True, kw_only=True)
class BookPlanCoinRow(_Row):
    """One coin's plan item at a rebalance (``book_plan_coins``). The plan id is set on write."""

    instrument_id: str
    eligible: bool
    signal: int
    vol: Decimal | None
    weight: Decimal
    target_notional: Decimal
    close_date: date | None

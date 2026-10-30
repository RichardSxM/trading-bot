"""Text enums stored in Postgres, each mirrored by a named CHECK in ``models.py`` (spec 0002).

Nautilus enums are stored as their member names in snake case. Their members are listed by
hand so this package never imports NautilusTrader; a test in the trader image compares them
with the pinned Nautilus 1.231.0 enums.
"""

from enum import StrEnum


class Env(StrEnum):
    """Trading environment a row belongs to; never mixed across a parent link."""

    DEMO = "demo"
    LIVE = "live"


class InstrumentKind(StrEnum):
    """Kind of instrument traded; only perpetual futures for now."""

    PERPETUAL = "perpetual"


class StrategyKind(StrEnum):
    """Family a strategy belongs to."""

    SCALPING = "scalping"
    HOUR_LEVEL = "hour_level"


class Stage(StrEnum):
    """Promotion stage a strategy version moves between."""

    CANDIDATE = "candidate"
    PAPER = "paper"
    LIVE = "live"


class Verdict(StrEnum):
    """Outcome of a promotion decision."""

    PASS = "pass"
    FAIL = "fail"
    # Spec 0015: the evidence could not decide (a missing metric, cost, run, or too few trades).
    INSUFFICIENT = "insufficient"


class DecidedBy(StrEnum):
    """Who made a promotion decision."""

    GATE = "gate"
    MANUAL = "manual"


class StudyLab(StrEnum):
    """Which lab a study runs in (spec 0015)."""

    CANDLE = "candle"
    REPLAY = "replay"


class StudyRole(StrEnum):
    """What a run is for inside its study (spec 0015)."""

    DEVELOPMENT = "development"
    HOLDOUT = "holdout"
    BENCHMARK = "benchmark"


class EquityBasis(StrEnum):
    """How a daily equity close or a drawdown was valued (spec 0015 AC-6)."""

    MARKED = "marked"
    REALIZED = "realized"


class RunMode(StrEnum):
    """How a strategy run executes."""

    BACKTEST = "backtest"
    REPLAY = "replay"
    PAPER = "paper"
    LIVE = "live"


SIM_MODES = frozenset({RunMode.BACKTEST, RunMode.REPLAY})
"""Modes whose runs are written as one bundle by ``write_run_bundle``."""


class RunStatus(StrEnum):
    """Lifecycle of a strategy run: ``running`` then one terminal status."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL_RUN_STATUSES = (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ABORTED)
"""Statuses a run ends in; a ``study_runs`` row carries one (spec 0015)."""


class Sample(StrEnum):
    """Data window a run result was computed over."""

    IN_SAMPLE = "in_sample"
    OUT_OF_SAMPLE = "out_of_sample"
    FULL = "full"


class Intent(StrEnum):
    """What a strategy decision wants to do."""

    ENTER = "enter"
    EXIT = "exit"
    ADJUST = "adjust"


class RiskVerdict(StrEnum):
    """The risk check's answer to a decision."""

    APPROVED = "approved"
    REJECTED = "rejected"


class OrderOrigin(StrEnum):
    """What caused an order; only ``strategy`` orders must carry a decision."""

    STRATEGY = "strategy"
    KILL_SWITCH = "kill_switch"
    RECONCILIATION = "reconciliation"
    EXTERNAL = "external"


class OrderSide(StrEnum):
    """Nautilus ``OrderSide`` (without ``NO_ORDER_SIDE``)."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """Nautilus ``OrderType``."""

    MARKET = "market"
    LIMIT = "limit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"
    MARKET_TO_LIMIT = "market_to_limit"
    MARKET_IF_TOUCHED = "market_if_touched"
    LIMIT_IF_TOUCHED = "limit_if_touched"
    TRAILING_STOP_MARKET = "trailing_stop_market"
    TRAILING_STOP_LIMIT = "trailing_stop_limit"


class TimeInForce(StrEnum):
    """Nautilus ``TimeInForce``."""

    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    GTD = "gtd"
    DAY = "day"
    AT_THE_OPEN = "at_the_open"
    AT_THE_CLOSE = "at_the_close"


class OrderStatus(StrEnum):
    """Nautilus ``OrderStatus`` plus ``unknown`` for a stub order created from an orphan fill."""

    INITIALIZED = "initialized"
    DENIED = "denied"
    EMULATED = "emulated"
    RELEASED = "released"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CANCELED = "canceled"
    EXPIRED = "expired"
    TRIGGERED = "triggered"
    PENDING_UPDATE = "pending_update"
    PENDING_CANCEL = "pending_cancel"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    VOIDED = "voided"
    UNKNOWN = "unknown"


CLOSED_ORDER_STATUSES = frozenset(
    {OrderStatus.DENIED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.FILLED}
)
"""Statuses the open orders index leaves out (spec 0002); every other status counts as open."""


class OrderEventKind(StrEnum):
    """Nautilus order event class names in snake case."""

    ORDER_INITIALIZED = "order_initialized"
    ORDER_DENIED = "order_denied"
    ORDER_EMULATED = "order_emulated"
    ORDER_RELEASED = "order_released"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELED = "order_canceled"
    ORDER_EXPIRED = "order_expired"
    ORDER_TRIGGERED = "order_triggered"
    ORDER_PENDING_UPDATE = "order_pending_update"
    ORDER_PENDING_CANCEL = "order_pending_cancel"
    ORDER_MODIFY_REJECTED = "order_modify_rejected"
    ORDER_CANCEL_REJECTED = "order_cancel_rejected"
    ORDER_UPDATED = "order_updated"
    ORDER_FILLED = "order_filled"


class Liquidity(StrEnum):
    """Nautilus ``LiquiditySide``; ``NO_LIQUIDITY_SIDE`` is stored as ``no_liquidity``."""

    MAKER = "maker"
    TAKER = "taker"
    NO_LIQUIDITY = "no_liquidity"


class PositionSide(StrEnum):
    """Nautilus ``PositionSide`` (without ``NO_POSITION_SIDE``)."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class PositionStatus(StrEnum):
    """A position row is open until it closes; a reopen is a new row."""

    OPEN = "open"
    CLOSED = "closed"


class Granularity(StrEnum):
    """Snapshot resolution: ``raw`` as captured, ``hourly`` after downsampling."""

    RAW = "raw"
    HOURLY = "hourly"


class FundingSource(StrEnum):
    """Where a funding payment came from."""

    KRAKEN_LOG = "kraken_log"
    COMPUTED = "computed"


class RiskEventKind(StrEnum):
    """What a risk event reports."""

    ORDER_REJECTED = "order_rejected"
    LIMIT_HIT = "limit_hit"
    HALT = "halt"
    RESUME = "resume"
    STALE_FEED = "stale_feed"
    RECONCILE_MISMATCH = "reconcile_mismatch"
    RECORD_CONFLICT = "record_conflict"
    ORPHAN_FILL = "orphan_fill"


class Severity(StrEnum):
    """How urgent a risk event is."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ControlCmd(StrEnum):
    """Command the dashboard sends the trader over the ``control`` stream."""

    STOP = "stop"
    RESUME = "resume"
    FLATTEN = "flatten"
    PAUSE = "pause"
    UNPAUSE = "unpause"
    RECHECK = "recheck"
    DRILL_STEP = "drill_step"


class ReconcileTrigger(StrEnum):
    """What started a reconciliation check (spec 0023); a ``recheck`` command runs a ``periodic`` one."""

    START = "start"
    RECONNECT = "reconnect"
    PERIODIC = "periodic"


class ReconcileOutcome(StrEnum):
    """How a reconciliation check ended (spec 0023)."""

    CLEAN = "clean"
    MISMATCH = "mismatch"
    FAILED = "failed"


class MismatchKind(StrEnum):
    """One kind of reconciliation finding (spec 0023 AC-6 to AC-12)."""

    UNKNOWN_ORDER = "unknown_order"
    LOCAL_ONLY_ORDER = "local_only_order"
    UNKNOWN_POSITION = "unknown_position"
    POSITION_MISMATCH = "position_mismatch"
    MISSING_STOP = "missing_stop"
    STOP_SIZE_MISMATCH = "stop_size_mismatch"
    DUPLICATE_ENTRY = "duplicate_entry"


class MismatchAction(StrEnum):
    """What the trader did about a finding; ``none`` when it only reported (demo, flattening, halted)."""

    CANCELLED = "cancelled"
    FLATTENED = "flattened"
    CLOSED_AT_MARKET = "closed_at_market"
    STOP_REPLACED = "stop_replaced"
    ADOPTED = "adopted"
    MARKED_CANCELED = "marked_canceled"
    NONE = "none"


class ActionResult(StrEnum):
    """How a finding's action went."""

    OK = "ok"
    FAILED = "failed"
    PENDING = "pending"


class AlertCmd(StrEnum):
    """Command the dashboard sends the alerter over the ``control:alerts`` stream (spec 0013)."""

    MUTE = "alerts_mute"
    UNMUTE = "alerts_unmute"
    TEST = "alerts_test"


class ControlSource(StrEnum):
    """Who sent a control command: you from the dashboard or the command line, or a deploy (spec 0005, 0010)."""

    DASHBOARD = "dashboard"
    DEPLOY = "deploy"
    CLI = "cli"


class ControlOutcome(StrEnum):
    """What happened to a control command: ``received`` then one final outcome."""

    RECEIVED = "received"
    EXECUTED = "executed"
    EXPIRED = "expired"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class JournalKind(StrEnum):
    """Kind of a journal export row."""

    FILL = "fill"
    FEE = "fee"
    FUNDING = "funding"

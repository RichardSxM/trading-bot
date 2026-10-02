"""What the top bar shows for live values (spec 0004 AC-9), as pure functions of their inputs."""

from dataclasses import dataclass
from typing import Final, Literal

from trading_core.snapshot.envelope import SnapshotFormatError, decode

Level = Literal["ok", "warn", "bad", "lost"]

NS_PER_S: Final = 1_000_000_000


@dataclass(frozen=True)
class AgeView:
    """The data age label and its color level; ``skewed`` marks a heartbeat from the future."""

    text: str
    level: Level
    skewed: bool = False


NO_HEARTBEAT: Final = AgeView("no heartbeat", "bad")
BAD_SNAPSHOT: Final = AgeView("bad snapshot", "bad")
LINK_LOST: Final = AgeView("link lost", "lost")


def age_state(now_ns: int, ts_ns: int, amber_s: float, red_s: float) -> AgeView:
    """Neutral up to ``amber_s``, amber above it, red above ``red_s``; a negative age shows ``0s``."""
    age_ns = now_ns - ts_ns
    if age_ns < 0:
        return AgeView("0s", "ok", skewed=True)
    age_s = age_ns / NS_PER_S
    level: Level = "bad" if age_s > red_s else "warn" if age_s > amber_s else "ok"
    return AgeView(f"{int(age_s)}s", level)


def heartbeat_view(raw: str | bytes | None, now_ns: int, amber_s: float, red_s: float) -> AgeView:
    """The age view for the raw ``{env}:snapshot:heartbeat`` value (None when the key is missing)."""
    if raw is None:
        return NO_HEARTBEAT
    try:
        snapshot = decode(raw)
    except SnapshotFormatError:
        return BAD_SNAPSHOT
    return age_state(now_ns, snapshot.ts_ns, amber_s, red_s)

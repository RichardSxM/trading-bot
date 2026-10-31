"""The start check that every paper coin is in the instrument cache (spec 0025 AC-15).

The Kraken data client loads its instruments when it connects, which happens after the strategies
are added to the node. Actors start before strategies, so this actor, registered before the
recording actor and the strategies, sees the loaded cache before any strategy starts. A coin the
venue does not list (for example ``PF_PEPEUSD`` missing from the demo venue) logs one critical line
naming it and calls ``refuse``, which stops the node so the trader exits 1.
"""

import logging
from collections.abc import Callable, Sequence

from nautilus_trader.common.actor import Actor
from nautilus_trader.config import ActorConfig
from nautilus_trader.model.identifiers import InstrumentId

log = logging.getLogger(__name__)


def missing_instruments(instrument_ids: Sequence[str], loaded: Callable[[InstrumentId], bool]) -> list[str]:
    """The ids in ``instrument_ids`` that ``loaded`` does not find, in the given order."""
    return [i for i in instrument_ids if not loaded(InstrumentId.from_str(i))]


class InstrumentCheck(Actor):  # type: ignore[misc]  # Actor is Cython with no stubs
    """Refuses the start when a paper coin is not in the instrument cache."""

    def __init__(self, instrument_ids: Sequence[str], refuse: Callable[[], None]) -> None:
        super().__init__(ActorConfig(component_id="InstrumentCheck"))
        self._instrument_ids = tuple(instrument_ids)
        self._refuse = refuse

    def on_start(self) -> None:
        """Check the cache once; on a missing coin, log it and refuse."""
        missing = missing_instruments(self._instrument_ids, lambda i: self.cache.instrument(i) is not None)
        if not missing:
            return
        log.critical(
            "a paper coin is not in the instrument cache, refusing to start",
            extra={"fields": {"code": "paper_instrument_missing", "instrument_ids": missing}},
        )
        self._refuse()

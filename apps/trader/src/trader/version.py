"""``python -m trader.version`` (spec 0015 AC-15): the strategy version this image would start, as JSON.

Prints ``{strategy, kind, config, code_hash, frozen_hash}`` from the trader's own settings and the
installed source, without starting the node or touching a database, so a version can be promoted by
hand (``gate promote --manual``) before its deploy. Exit 78 on bad config.
"""

import json
import sys
from typing import NoReturn

from pydantic import ValidationError

from trader.main import EXIT_CONFIG, TraderSettings
from trading_core import versioning
from trading_core.node.placeholder import STRATEGY_NAME, PlaceholderTimed
from trading_core.recording.enums import StrategyKind
from trading_core.recording.rows import JsonObject, normalize_json


def describe(settings: TraderSettings) -> JsonObject:
    """The version the trader records at start: its strategy, kind, config, and both hashes."""
    config = settings.strategy_config()
    frozen = versioning.freeze(PlaceholderTimed.__module__, config)
    return {
        "strategy": STRATEGY_NAME,
        "kind": StrategyKind.SCALPING.value,
        "config": normalize_json(config),
        "code_hash": frozen.code_hash,
        "frozen_hash": frozen.frozen_hash,
    }


def main() -> NoReturn:
    """Print the version JSON, or the config errors and exit 78."""
    try:
        settings = TraderSettings()  # fields come from the environment
    except ValidationError as exc:
        print(json.dumps({"errors": exc.errors(include_url=False, include_input=False)}, default=str), file=sys.stderr)
        sys.exit(EXIT_CONFIG)
    print(json.dumps(describe(settings), indent=2, sort_keys=True))
    sys.exit(0)


if __name__ == "__main__":
    main()

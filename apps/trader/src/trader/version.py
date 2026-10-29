"""``python -m trader.version`` (spec 0015 AC-15): the strategy version this image would start, as JSON.

Prints ``{strategy, kind, config, code_hash, frozen_hash, git_commit}`` from the trader's own settings and the
installed source, without starting the node or touching a database, so a version can be promoted by
hand (``gate promote --manual``) before its deploy, and ``kfs-deploy`` feeds it to ``gate check-version``
(AC-26). Exit 78 on bad config.
"""

import json
import sys
from typing import NoReturn

from pydantic import ValidationError

from trader.main import EXIT_CONFIG, TraderSettings
from trader.paper_strategy import paper_version
from trading_core.recording.rows import JsonObject, normalize_json


def describe(settings: TraderSettings) -> JsonObject:
    """The version the trader records at start: its strategy, kind, config, both hashes, and the image's commit."""
    version = paper_version(settings)
    return {
        "strategy": version.name,
        "kind": version.kind.value,
        "config": normalize_json(version.config),
        "code_hash": version.frozen.code_hash,
        "frozen_hash": version.frozen.frozen_hash,
        "git_commit": settings.git_commit,
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

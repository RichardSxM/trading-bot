"""Spec 0020 AC-1 and AC-13 hash check: the code hash of every trading_core.node module closure under a root.

Prints one `module hash` line per module, sorted. Run it on a `git archive` of main and on the branch and
compare the lines (spec 0019's close out did the same by hand):
  git archive origin/main packages/core/src | tar x -C "$S"
  KFS_TAG=<tag> docker compose --profile lab run --rm --no-deps -T -v "$S/packages/core/src:/main-src:ro" \
    -v "$PWD/scripts/spikes:/spike:ro" --entrypoint python lab -I /spike/hash_check.py /main-src/trading_core
Without an argument it hashes the image's own trading_core (the branch, once the image is rebuilt).
"""

import sys
from pathlib import Path

from trading_core.versioning import code_closure, code_hash, package_root


def main(root: Path) -> None:
    for path in sorted((root / "node").glob("*.py")):
        if path.stem == "__init__":
            continue
        module = f"trading_core.node.{path.stem}"
        print(module, code_hash(code_closure(module, root)))


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else package_root())

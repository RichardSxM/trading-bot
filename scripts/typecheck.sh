#!/bin/sh
# mypy strict over every workspace package, one run per package (each has its own tests/,
# and their test file names overlap). Needs NautilusTrader importable, so on a Mac it hands
# off to the Linux `tools` container; on Linux (the container, CI) it runs mypy directly.
set -eu
cd "$(dirname "$0")/.."

if [ "$(uname -s)" != "Linux" ]; then
    exec docker compose --profile tools run --rm --no-deps -T tools scripts/typecheck.sh
fi

# Tests get a second pass with only the annotation rules relaxed; their bodies are still checked.
status=0
for pkg in packages/core apps/trader apps/recorder apps/dashboard apps/lab apps/alerter; do
    echo "mypy: $pkg"
    mypy "$pkg/src" || status=1
    mypy --allow-untyped-defs --allow-incomplete-defs --allow-untyped-calls "$pkg/tests" || status=1
done
exit $status

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
# The two passes use their own cache dirs: mypy drops cached modules whose options differ, so one
# shared cache was rebuilt from scratch on every run.
cache="${MYPY_CACHE_DIR:-.mypy_cache}"
status=0
for pkg in packages/core apps/trader apps/recorder apps/dashboard apps/lab apps/alerter apps/gate; do
    echo "mypy: $pkg"
    mypy --cache-dir "$cache/src" "$pkg/src" || status=1
    mypy --cache-dir "$cache/tests" --allow-untyped-defs --allow-incomplete-defs --allow-untyped-calls "$pkg/tests" || status=1
done
exit $status

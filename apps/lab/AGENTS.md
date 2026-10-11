# lab (apps/lab)

## Overview

The laptop only replay lab (spec 0008). It replays recorded BTC order books (our own catalog from the recorder, or free Tardis days) through the Nautilus backtest engine, with a pessimistic queue, latency, maker and taker fees, and fill quality measured. Strategies run exactly as the paper trader builds them, so a replay and the paper loop share one code path. It never runs on the VPS and is never deployed.

## Key files

| File | Owns |
|---|---|
| `src/lab/main.py` | The `lab` command (`replay`, `import-tardis`, `parity`, `prune`) and every exit code |
| `src/lab/runfile.py` | The TOML run file: validated before anything is read or written, every problem listed (exit 64) |
| `src/lab/replay.py` | Segment plan, variants, the bundle written into Postgres in one transaction, reports in variant order |
| `src/lab/units.py` | One unit: one variant over one segment in a fresh engine (venue, fees, latency, queue model) |
| `src/lab/pool.py` | One spawned process per unit, at most `LAB_WORKERS`; a failed unit fails only its variant |
| `src/lab/stream.py` | The hour stream: deltas grouped into feed messages, merged with trades, a `QuoteTick` after every top of book change |
| `src/lab/actors.py` | `LabRecorder` (the trader's `RecordingActor`, orders and positions only), `UnitRows`, `SegmentEndCloser` |
| `src/lab/strategies.py`, `probes.py` | The strategy registry; `raise_probe` exists only with `LAB_TEST_STRATEGIES=1` |
| `src/lab/tardis_import.py` | Free Tardis days into spec 0007's catalog layout under `tardis/`, the manifest written last |
| `src/lab/parity.py`, `paper_csv.py` | `lab parity`: the paper strategy replayed in memory against the CSV from `scripts/fetch-paper-decisions.sh` |
| `runs/*.toml` | The committed run files (baked into the image) |

Pure rules (segments, variants, metrics, markouts, fill stats, funding, the loser band, parity compare, book diff) live in `packages/core/src/trading_core/lab/`. Keep new logic there and these files thin.

## Commands

```bash
GIT_COMMIT=$(git rev-parse HEAD) docker compose --profile lab build lab   # without GIT_COMMIT, replay refuses (exit 78)
docker compose run --rm lab replay runs/random_taker.toml [--queue-bound] [--sweep-latency] [--pair-entry]
docker compose run --rm --no-deps lab import-tardis 2026-09-01 2026-09-01   # first of month days only
docker compose run --rm --no-deps lab parity runs/placeholder_paper.toml /lab-reports/paper/<from>_<to>.csv
docker compose run --rm --no-deps lab prune [--source tardis|own]
docker compose --profile lab run --rm --no-deps -T --entrypoint pytest lab -q           # unit tests
docker compose --profile lab run --rm -T --entrypoint pytest lab -q -m postgres         # tests that need Postgres
```

## Conventions

- Demo only: `TRADING_ENV=live` exits 78 before anything else (AC-17). It connects to Postgres as `lab_app`, and only `replay` connects at all, so the other commands take `--no-deps`.
- Same inputs give the same output (AC-12): `PYTHONHASHSEED=0` is set on this service only, and reports hold nothing that differs between runs except the run and batch ids and the start and end times. A change that touches replay output needs a determinism test.
- A strategy is never subclassed or patched for the lab; register the paper loop's own class and config in `strategies.py`.
- Lab catalog data lives in the git ignored `./catalog-lab` (`tardis/` and `own/`), reports in `./lab-reports`. The whole catalog is capped at `LAB_CATALOG_CAP_GB` (40 GB, exit 3 when an import would pass it).
- Run files are baked into the image, so a new or edited one needs a rebuild. Put scratch run files under `lab-reports/scratch/` and pass that path.

## Gotchas

- Every hour after a segment's first opens with the compactor's synthetic book snapshot; the stream drops that run of rows so resting orders keep their queue place. Any other `CLEAR` raises `UnexpectedSnapshotError`.
- Our own catalog days can be copied straight from the VPS without B2: `ssh kfs@kfs-vps 'sudo -n kfs-compose exec -T compactor sh -c "cd /data/catalog && tar cf - <files>"' | tar xf - -C catalog-lab/own`.
- Never run a big B2 download (`scripts/fetch-catalog.sh`) between 00:00 and 02:30 UTC: the download cap is shared with the VPS nightly backup.
- `mem_limit: 3g` fits two workers in the laptop's 4 GB Docker VM; raise `LAB_WORKERS` only with more memory (AC-16).

## Related specs

- [0008 scalping simulator](../../docs/specs/0008-scalping-simulator/index.md) (with `verify.md`)
- [0007 order book recorder](../../docs/specs/0007-order-book-recorder/index.md), for the catalog layout

_Drafted by /audit from the repo, worth a quick human pass. Edit freely: once a line stops matching this draft, later runs treat it as curated and will flag rather than overwrite it._

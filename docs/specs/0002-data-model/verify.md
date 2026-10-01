# 0002. Verify: data model

Steps you may run on the laptop stack (`TRADING_ENV=demo`) to prove the build against [index.md](index.md). Each step names the criteria it checks.

1. **Build and migrate.** `docker compose build`, then `docker compose run --rm migrate`. Expect the role setup log line, then Alembic running `0002_data_model`. (AC-1, AC-13)
2. **Tables and roles exist.** `docker compose exec postgres psql -U trading -d trading -c '\dt'` lists 18 tables; `-c '\du'` lists `trader_app`, `dashboard_app`, `backup_app`. (AC-1, AC-11)
3. **One head.** `docker compose run --rm migrate alembic heads` prints only `0002_data_model`. (AC-1)
4. **Integration tests.** `docker compose run --rm -T migrate pytest -q -m integration` passes (dev image target; it runs as the owner role on the Compose network and creates and drops its own throwaway database). (AC-1 to AC-15)
5. **Role denials by hand.** Connect as `dashboard_app` and try `INSERT INTO fx_rates …`: expect permission denied. As `trader_app`, `DELETE FROM fills`: denied. As `backup_app`, `SELECT downsample_snapshots();`: returns a count. (AC-11, AC-12)
6. **Missing password fails closed.** Blank `POSTGRES_DASHBOARD_PASSWORD` in `.env` and rerun migrate: exit code 1 and one JSON error log. Restore it afterwards. (AC-13)
7. **Round trip the migration.** `docker compose run --rm migrate alembic downgrade 0001_baseline`, then `docker compose run --rm migrate` again: both succeed, and step 2 matches again. (AC-1)
8. **Stack still boots.** `docker compose up -d`: `trader` and `dashboard` reach healthy while connected as their own roles. (AC-11)

## Value sourcing (added by /develop, 2026-10-01)

Each step checks where one value comes from, at the edge that breaks if the source is wrong.

9. **env from TRADING_ENV.** In a demo process, write a decision with `env = live` through the write API: expect `EnvMismatchError`, and no row. (AC-4)
10. **`_at` derived from `_ns`.** Record a fill with `filled_ns = 1727000000123456789`: the stored `filled_at` is `2024-09-22 10:13:20.123456+00`, truncated to microseconds in UTC. (AC-9)
11. **git_commit from the image.** `GIT_COMMIT=$(git rev-parse HEAD) docker compose build`, then call `ensure_strategy_version` from the trader: the stored `git_commit` is that commit, not `unknown`. A build without the variable records `unknown`, and a live `start_run` on it raises `UnknownCommitError`. (AC-15)
12. **version_hash from canonical JSON.** Call `ensure_strategy_version` twice with the same config, keys in a different order, and `Decimal("0.00000001")` in it: both return the same version id, and the stored config holds `"0.00000001"` (not `1E-8`).
13. **Live gate reads the latest promotion.** Record `paper → live` with `pass` on day 1 and `fail` on day 2: a live `start_run` raises `PromotionRequiredError`; add a `pass` on day 3 and it starts. (AC-15)

14. **Journal amount is a signed cash flow.** Buy 0.002 at 60000.5 with fee 0.06, sell 0.002 at 60100 with fee 0.0601, plus a funding row of -0.0123, all in one range: the rows are `fill -120.0010`, `fee -0.06`, `funding -0.0123`, `fill 120.2000`, `fee -0.0601`, and they sum to `0.0666` (gross minus fees plus funding). (AC-14)
15. **Fee rows follow the stored fee.** A rebate of `-0.01` gives a fee row of `+0.01`; a zero fee gives no fee row; a fee in `BTC` gives a `BTC` row with `eur_amount` NULL and `fx_missing`. (AC-14)
16. **EUR rate from the event's UTC date.** With rates only on Friday (1.20) and Monday (1.30): a Saturday 23:59 UTC fill uses 1.20, a Monday 00:00 UTC fill uses 1.30, a Thursday fill before any rate gives `fx_missing`. (AC-14)
17. **Range and runs.** A fill exactly at `start` is in, one exactly at `end` is out; a backtest run's fill is out; a fill on a stub order is in. A naive or `+01:00` bound raises `ValueError`. (AC-14)
18. **Inverse instrument refused.** A fill on an `is_inverse` instrument inside the range raises `UnsupportedInstrumentError` for the whole call; a range without it works. (AC-14)
19. **Run bundle is all or nothing.** A backtest bundle with one strategy order missing its decision raises and leaves no run, order, or fill behind; a `paper` bundle raises `InvalidModeError`. (AC-10)
20. **Replays leave one row.** Write each order event, snapshot, funding payment, risk event, run result, and control command twice: one row each, no error. (AC-5)

Steps 14 to 20 are automated in `tests/integration/test_journal.py` and `tests/integration/test_full_api.py` (step 4 runs them).

Deferred: `orders.decision_id` from the order tags (feature 6's mapper).

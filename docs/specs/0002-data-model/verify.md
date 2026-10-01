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

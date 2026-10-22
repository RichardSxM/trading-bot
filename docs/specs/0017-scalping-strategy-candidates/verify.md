# Verify: scalping strategy candidates · spec 0017 · updated 2026-10-22
_Results and verify steps for spec 0017. The audit record below was written before any candidate study ran (AC-3)._

## Audit record (AC-3)

- **Batch folder**: `lab-reports/audit-20261021/` (`audit.json`, `audit.md`), generated 2026-10-21T15:34:17Z over the 43 development days 2022-04-01 to 2025-10-01.
- **Pooled spread p95**: 6.2615 bps. Every child's replay run file sets `max_spread_bps = "6.27"` (rounded up to 2 places), never changed afterwards.
- **Median absolute 5 minute move**: 5.8884 bps (1 minute 2.4704, 15 minute 10.2615).
- **`reachable`**: `maker_entry` (7 bps, needs 10.5) false; `taker_entry` (10 bps, needs 15) false.
- **Run order**: all three candidates enter maker, so the flag warns about all of them and moves none (Order rule, step 2). The order stands: the four candle screens (`bollinger_rsi_screen`, `bollinger_rsi_screen_adx`, `vwap_retest_screen`, `vwap_retest_screen_adx`) first; then replay studies one at a time, `bollinger_rsi`, then `vwap_retest`, each only if its screen passed; then `delta_divergence` once its AC-17 pre check passes.

Warning, in plain words: the typical 5 minute move is smaller than the 10.5 bps a maker round trip needs to clear 1.5 times its cost. Only 28.5 % of 5 minute moves and 49.2 % of 15 minute moves reach 10.5 bps. A candidate has to pick its moments well to pass; the reward floor (`min_target_bps`) already refuses targets closer than that.

Notes for later (no effect on the order or the cap):

- The pooled p95 is driven by the thin 2022 days (day p95 up to 16.2 bps). Since 2023-10 every day's p95 is under 2 bps, so the 6.27 cap mostly binds on 2022 days.
- The per day mean absolute funding rate jumps by about five orders of magnitude between 2022-09-01 (0.24) and 2022-10-01 (0.0000055). This looks like a unit change in the recorded funding feed, not the market. No gate reads this column; check it before any later work uses funding.

## Both labs (AC-16)

Run 2026-10-21 to 2026-10-22 with `LAB_WORKERS=2`, branch code at 9464aa6 mounted into the lab container, scratch run files under `lab-reports/scratch/` (`min_target_bps` and `regime_adx_max` unset).

| Run | Range | Exit | Closed trades | Net P&L (USD) | Batch |
|---|---|---|---|---|---|
| `lab replay` `bollinger_rsi` | 2025-09-01 | 0 | 4 | -10.08 | `01a160d9-acff-7255-9fcf-76eff9e421e3` |
| `lab replay` `vwap_retest` | 2025-09-01 | 0 | 9 | -14.26 | `01a16135-692d-732b-8580-7384ab6d0dcf` |
| `lab replay` `delta_divergence` | 2025-09-01 | 0 | 14 | -13.05 | `01a16158-6100-7712-99b6-56dff9efabd9` |
| `lab backtest` `bollinger_rsi` | 2025-06 | 0 | 3 | 7.89 | `01a16188-ee3e-7703-b0fa-112d133ecc3a` |
| `lab backtest` `vwap_retest` | 2025-06 | 0 | 2 | 5.83 | `01a1618a-671c-7597-b93f-9f8b3dbeaa9b` |
| `lab backtest` `delta_divergence` | 2025-06 | 64 | none | none | refused: "is a scalping strategy; lab backtest runs hour_level strategies and the scalping candidates with a candle screen" |

Determinism rerun: `bollinger_rsi` over 2025-09-01 again (batch `01a1618c-4932-742e-9540-9f7f9ad262cb`). Its `base.json` matches the first run field for field, apart from `batch_id`, `run_id`, `started_at`, `ended_at`, and `run_config.git_commit`. Result: **pass**. These are scratch runs, so P&L means nothing here.

Heads up on `git_commit`: the two runs recorded `bd8cd13` and `0ffc03d`, neither of which is this branch. The value is baked into the shared lab image at build time, and other sessions rebuilt it between runs. The code that ran was this branch's (it was mounted), so the result holds, but the label is wrong. Before any study runs, rebuild the lab image from this branch with `GIT_COMMIT` set, so study reports carry the right commit.

## Delta trade rate pre check (AC-17)

Run file `lab-reports/scratch/delta_divergence_precheck.toml` (sha256 `30e31d8f…2433`): `lookback_bars = 5`, `move_atr = "1.5"`, `min_target_bps = "10.5"`, `regime_adx_max = "25"`, `max_spread_bps = "6.27"`, one `lab replay` per day.

| Day | Approved | `blocked:regime` | `target_too_close` | `invalid_stop` | Closed trades | Batch |
|---|---|---|---|---|---|---|
| 2022-10-01 | 6 | 65 | 155 | 2 | 3 | `01a161be-ac1e-7213-9dcc-f5990466572c` |
| 2023-10-01 | 0 | 151 | 98 | 0 | 0 | `01a161c9-4d4b-739e-8111-1b833facf343` |
| 2024-10-01 | 5 | 63 | 29 | 0 | 4 | `01a161d6-9b87-753f-baef-a8869b94ec04` |
| 2025-10-01 | 0 | 50 | 54 | 0 | 0 | `01a161f9-b0b5-7405-9001-7f427d6c7b3d` |

Approved `enter` decisions average 2.75 a day (11 over 4 days), under the 17 needed. **`delta_divergence`: not run: trade rate.** Its replay study does not run.

In plain words: the signal fires often enough (about 60 to 250 times a day), but the reward floor and the ADX regime filter refuse nearly all of it. That fits the audit's warning above. Changing the candidate is a job for an `/architect` amendment (one revision per candidate), not this build.

## Candle screens (AC-9, task 13)

### Void: `bollinger_rsi_screen` (2026-10-22, before AC-18)

Started 08:53 UTC on `kfs/lab:s0017` (commit 8c7a852) and stopped by hand at about 10:15 UTC. In early February 2023 a maker entry filled while the bar's quote was already past the stop, so the stop was rejected ("in the market") and `STOP_NOT_ACCEPTED` blocked entries for the rest of the run. 17,232 later signals were refused with `blocked:stop_not_accepted`.

| Grid point | Run | Trades (2.8 years) | Net P&L (USD) |
|---|---|---|---|
| grid-1 | `597be33c-e5d6-4bba-bbf4-a146451d3e69` | 10 | 1.41 |
| grid-2 | `0d4eee3d-c7f4-4465-ac03-d0db5913963c` | 9 | −37.37 |
| grid-3, grid-4 | stopped partway | | |

No `lab screen` ran on it. Its rows stay in Postgres and still count toward `bollinger_rsi`'s trial count N. Spec amendment AC-18 (commit 00d5586) fixed the base in e6c21ac, and the screen reruns as `bollinger_rsi_screen_v2` (index task 12b).

### Void: `bollinger_rsi_screen_v2` (2026-10-22, fill check only)

Started 11:01 UTC on `kfs/lab:s0017` (commit e6c21ac, batch `01a16348-352e-76c2-95d2-a1488dcc3388`) and stopped by hand at about 11:45 UTC, with no grid point finished. The fill check fired once, but 4 stops were still rejected as in the market, one in each grid point (for example a sell stop at 22966 with the venue's bid at 22955, 2023-02-01). The backtest engine shows each quote to the simulated venue before the strategy, so the strategy's cached quote still allowed the stop. 19,288 signals were refused with `blocked:stop_not_accepted`.

No `lab screen` ran on it, and its rows count toward N like the first. The second AC-18 amendment (the next quote settles a rejected stop, index task 12c) fixed it, and the screen reruns as `bollinger_rsi_screen_v3`.

## Test notes

- `apps/lab/tests/test_candle_failure.py::test_sigterm_stops_every_unit_writes_interrupted_reports_and_exits_130` fails (exit 1, not 130) when the machine is loaded, on `main` as well as this branch. It sends SIGTERM on a fixed 2 second timer, which races the backtest's setup. This branch doesn't cause it; a fix belongs in its own change.

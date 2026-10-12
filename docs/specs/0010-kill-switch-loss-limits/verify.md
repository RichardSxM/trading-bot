# 0010. Verify: kill switch and loss limits

These are steps you may run to check the feature by hand, on the laptop in demo, after `docker compose build` and `docker compose up -d`. Bring the laptop stack down when you are done (the VPS runs the app). Each step names the acceptance criteria it checks. Run them in order; later steps assume the earlier ones passed.

To get a position open without waiting for the next 15 minute boundary, you may set `PLACEHOLDER_INTERVAL_MIN=5` and `PLACEHOLDER_HOLD_MIN=4` in your laptop override for this run.

Commands below use the CLI as `cli`, meaning `docker compose exec -T dashboard python -m dashboard.control_cli`.

1. **Stop when flat (AC-1, AC-3, AC-5, AC-7, AC-17).** With no position open, tap the red stop button.
   - The command state goes `stop sent · received by trader · confirmed` within a few seconds.
   - The top bar shows `HALTED · manual stop · 0s`.
   - `redis-cli GET demo:trader:halt` shows the JSON with `reason` `manual_stop` and `source` `dashboard`.
   - `risk_events` has one `halt` row, severity `warning`.
2. **Entries refused while halted (AC-15).** Wait for the next boundary. The trader log shows the decision refused with `halted`, there is a `limit_hit` `info` event, and no order is sent.
3. **Halt survives a restart (AC-5, AC-20).** Run `docker compose restart trader`. The top bar still shows HALTED, and `trader_state.halted` is true.
4. **Resume (AC-4, AC-18).** On the System page, press Resume.
   - A confirm step appears. Cancel it and check nothing was sent.
   - Press Resume again and confirm. The result is `confirmed`, the badge goes, `trading_state` is `ACTIVE`, both keys are gone, and there is one `resume` event.
   - Repeat this step with JavaScript turned off in the browser. It still works.
5. **Stop with a position open (AC-1, AC-3).** Wait until a position is open with its protective stop, then tap stop.
   - Within about 5 s: the result is `confirmed`.
   - `trader_state` shows `open_positions` 0, `open_entry_orders` 0, and `protective_stops` 0.
   - The closing order in `orders` has `origin = kill_switch`.
   - The stop order was cancelled after the position closed, not before (compare the `order_events` times).
6. **Resume while not flat (AC-3, AC-4).** Set `SANDBOX_REJECT_KILL_SWITCH_ORDERS=true` and recreate the trader. Wait for a position, then tap stop.
   - After 20 s the result is `Stop failed` with reason `flatten timeout`, and there is a `critical` event `flatten_timeout:<id>`.
   - The log shows the close retried every 1 s, and an `exit_failed` event after the 5th refusal.
   - Resume answers `failed · not flat yet`.
   - Wait for the protective stop to close the position (or set the drill back to `false` and recreate; the restart closes it as abandoned). Then Resume works.
7. **Flatten without halting (AC-2).** With a position open, press Flatten on the System page and confirm. The position closes, the result is `confirmed`, no HALTED badge appears, and the next boundary may enter again.
8. **Command line (AC-19).**
   - Run `cli stop --wait-result`. It prints `executed` and exits 0. The `{env}:control` entry has `source` `cli`.
   - Then `cli resume --wait-result` exits 0.
   - With Redis stopped, `cli stop` exits 2.
9. **Daily loss limit (AC-8, AC-9, AC-14, AC-17).** Set `RISK_DAILY_LOSS_LIMIT_USD=0.01` and recreate the trader. Wait for a position to open and go a cent into loss.
   - The trader halts with `daily_loss_limit` and flattens.
   - The `halt` event is `critical`.
   - The System page shows today's loss over the limit.
   - Resume: the halt lifts, but `entries_blocked` still lists `daily_loss_limit` in plain words.
   - Restart the trader: the block is still there.
   - Put the limit back afterwards.
10. **Total loss limit (AC-10, AC-21).**
    - Set `RISK_TOTAL_LOSS_LIMIT_USD=0.01` and `RISK_DAILY_LOSS_LIMIT_USD=0.01`, then recreate the trader. It halts with `total_loss_limit` once any loss exists since `RISK_TOTAL_LOSS_SINCE`.
    - Resume answers `failed · total loss limit`.
    - Set the total back to 100 and the daily to 20, then recreate. Resume now works.
    - Then set the total to 10 with the daily at 20: the trader refuses to start.
11. **Trade cap (AC-11).** Set `RISK_MAX_TRADES_PER_DAY=1` and recreate. After the first position of the day, the next boundary is refused with `blocked:trade_cap`, and there is one `limit_hit` `warning` event keyed `trade_cap:<today>`.
12. **Cooldown (AC-12).** With `RISK_LOSS_COOLDOWN_S=900`, after a losing close the dashboard shows the cooldown time left, and the next boundary is refused with `blocked:loss_cooldown`. Restart the trader within the wait, and the block is still there with the same end time.
13. **Postgres down at start (AC-13).** Stop `postgres`, then restart the trader.
    - `entries_blocked` shows `loss_unknown`, and the log shows `loss_seed_failed` every 30 s.
    - Start `postgres`: within 30 s the block clears.
14. **Bad halt key (AC-5).** Run `redis-cli SET demo:trader:halt notjson`, then restart the trader. It starts halted with reason `unknown`, and logs one `critical` `halt_key_invalid`. Resume clears it.
15. **Deploy leaves a halt alone (AC-20).** With the trader halted, run the deploy's pause and wait-flat path (`cli pause --source deploy --wait-result`, then `cli unpause --source deploy`). The halt key is still there afterwards.
16. **Done.** Put every setting back, then run `docker compose down`.

# Emergency runbook (one page)

Keep a copy on your phone. Rehearse it once a month in paper mode, and once before
going live. A runbook never practised is not a runbook.

## The bot is at L1 and I did nothing

Check the trigger first (`tradebot status`, or the dashboard pill):

```
daily_loss          -> it lifts by itself at the next trading day; do nothing
stale_feeds         -> it lifts after 10 min of healthy feeds; check the feed if not
exchange_error_rate -> it lifts after 10 min of a clean order rate; check the venue status page
reconciliation      -> it lifts after 10 min of clean reconciles; investigate the mismatch
disk_full           -> free space; it lifts once disk is under the warn threshold
weekly_loss         -> needs you: review the week, then `tradebot resume`
account_limit (other), manual, kill file, L2, L3 -> needs you
```

Every automatic change is in `kill_events` with trigger `auto_resume` and the reason.
## If something looks wrong, in this order

1. **Stop adding risk.** Do not open trades manually.
2. **Check state:** `python -m tradebot status`, then the dashboard, then the last 50
   lines of `logs/tradebot.log`.
3. **If the bot responds:** `python -m tradebot kill` (or `/flatten` on Telegram, or the
   dashboard Flatten button).
4. **If it does not respond:** create the `KILL` file (`touch KILL`) or run the CLI
   kill. Note: stopping the container does **not** cancel orders or close positions.
5. **Open the exchange app:** cancel all open orders, check balances and positions,
   close positions manually if needed.
6. **If key compromise is suspected:** revoke the API key immediately, then change the
   exchange password and 2FA.
7. **Write the incident entry** (template below).
8. **Do not restart** until the cause is understood and reconciliation is clean.

## Quick diagnosis

| Symptom | Check |
| --- | --- |
| No heartbeat alert but no trades | kill level, strategy pauses, `risk_veto` events |
| Bot silent | process alive? `logs/tradebot.log` tail, watchdog incident rows |
| Weird fills | spread, book depth at the time, `exit_submitted`/`fill` events |
| Reconciliation mismatch | exchange app: manual trade? open orders? balance change? |
| Clock drift | `timedatectl`, NTP |
| Disk | `df -h`, `/healthz`, disk warning events |
| After a deploy | last commit, config diff, startup event, preflight gates |

## Incident log template

```
time (UTC / IST):
mode (paper / testnet / live):
what I saw:
trigger (first failing check / kill trigger):
bot actions (from the event log):
my actions:
PnL impact:
root cause:
fix:
test added to prevent recurrence:
```

## Deploying while positions are open (live)

1. Announce on Telegram.
2. `pause` (L1); cancel pending entries, leave protective exchange-side stops.
3. Back up the database (`scripts/backup.sh` or `cp data/state/tradebot_live.db*`).
4. Deploy the new image.
5. On startup: reconcile with the exchange, run preflight (`tradebot live-check`).
6. Smoke test: replay the last hour and confirm sensible decisions.
7. `resume` (back to L0).
8. Watch for 30 minutes with alerts at WARN or above.

**Rollback:** keep the previous image tag. Migrations follow expand-then-contract, so
the old version can read the new schema. If reconciliation mismatches or the smoke
test fails, roll back and stay at L1.

**Canary rule:** any strategy or risk change runs in shadow (paper) mode for at least
three days before promotion.

## Re-validating before promoting anything

Run these in order. Each one is allowed to say "no" - that is what they are for.

```bash
python -m tradebot backtest --days 10            # single run + gates
python -m tradebot walk-forward --train-days 3 --test-days 1 --windows 3
python -m tradebot sensitivity --strategy S1_ema_trend --days 2
python -m tradebot robustness --days 4 --cost-multiplier 2
python -m tradebot parity --minutes 1200         # must print MATCH
python -m tradebot signals --days 1 --horizon 30 # screen + ladder + leakage checks
python -m tradebot slippage --days 2             # measured vs assumed costs
```

A strategy is promotable only if **all** of the following hold:

* OOS profit factor > 1.3, Sharpe > 1.0, max drawdown < 15%, at least 200 trades;
* still profitable in the 2x-cost *rerun* (not the post-hoc subtraction);
* MC p95 drawdown < 20%;
* sensitivity verdict is a plateau, and the best cell is profitable;
* parity prints `MATCH`;
* multiple-testing: the best Sharpe beats what luck would produce over the number of
  trials logged in the `experiments` table.

If any check fails, the honest action is to change the strategy, not the gate.

## Fire drill (paper, monthly)

```bash
python -m tradebot backtest --days 2     # get a session with open positions
python -m tradebot drill                 # kill + flatten, prints time-to-flat
```

Expected: all positions closed, kill level L2, time-to-flat well under 10 seconds.
Record the result and the date in the incident log.

## Contacts and links

```
exchange status page:        ______________________
exchange support:            ______________________
2FA backup codes stored at:  ______________________  (offline, not on the trading PC)
Telegram commands:           /status /pnl /positions /report /pause /flatten /kill
CLI commands:                tradebot status | pause | resume | kill | lockdown | drill
```

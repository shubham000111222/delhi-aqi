# Emergency runbook (one page)

Keep a copy on your phone. Rehearse it once a month in paper mode, and once before
going live. A runbook never practised is not a runbook.

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

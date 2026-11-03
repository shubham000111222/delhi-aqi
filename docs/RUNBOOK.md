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
python -m tradebot meta --days 1                 # does a meta-label filter add anything?
python -m tradebot stream --minutes 120          # streaming feed replay: contract holds?
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

## Execution cost and the size cap

The dashboard's execution panel and `tradebot slippage` read the same recorder: every fill
is measured against the price the decision was made at, bucketed by size-versus-depth.

After 30 fills the engine fits a slippage model from its own fills and caps each entry at
the largest size whose *predicted* p95 stays inside `execution.max_slippage_bps` (25 bps by
default; `0` disables the cap). On the synthetic book this cap never binds - the orders are
tiny next to the book - so a change in results after enabling it would mean something else
changed.

Read the p95, not the mean: a median of 0 bps with a long tail is a strategy that looks
fine until it is not.

## Logging a session to MLflow

`python -m tradebot run --mlflow ./mlruns` writes one run when the session ends: equity,
cash, realised and unrealised P&L, fees, trade count, win rate and the signal/veto/order/
fill counts as metrics; mode, capital and feed as params; and the exit kill level, mode
and feed list as tags. Nothing is written mid-session, so a crash loses the run - the
SQLite log is still the record of what happened, and `tradebot metrics` is still the
thing that judges it.

Read a store back with `python -m tradebot experiments --read ./mlruns`, or point MLflow
at it: `mlflow ui --backend-store-uri file://$PWD/mlruns`.

Do not read a session run's `win_rate` as a strategy result: it is a few dozen trades of
a reference strategy on synthetic data, and the gates in `tradebot metrics` are the only
thing allowed to call a strategy good.

## Meta-label gate (optional)

A meta-model decides *whether to take* a setup the strategies already produced. It cannot
resize one: the risk engine owns size. Install a card with:

```bash
python -m tradebot meta --days 3          # fit and evaluate; it prints a verdict
python -m tradebot run --meta-gate models/meta_btc.json
```

`save_meta` refuses to write a card whose gated trades do not make money after costs, so
in practice this flag has nothing to load on synthetic data - which is the honest state.
A card that is missing, malformed or inconsistent with its feature list stops the process
at startup instead of silently trading unfiltered.

At runtime the gate sees features rebuilt from the live bar window (cached per bar close).
If the feature store fails, the frame is missing or the model file breaks mid-session, the
entry is allowed and the reason is logged: a filter that fails closed looks exactly like a
market with no setups.

## Experiment tracking (MLflow)

Every backtest, grid cell and signal screen is already logged in the `experiments` table.
To compare them in MLflow:

```bash
python -m tradebot experiments                                  # what is logged
python -m tradebot experiments --export ./mlruns                 # write the file store
MLFLOW_ALLOW_FILE_STORE=true mlflow ui --backend-store-uri file://$PWD/mlruns
```

`MLFLOW_ALLOW_FILE_STORE=true` is not optional on MLflow 3: the file store is in
maintenance mode and refuses to open without it. The export does not need MLflow
installed; the flag is only for the UI.

A string metric - a verdict like `NOT PROMOTABLE` - is written as a tag, not a metric,
because MLflow rejects non-numeric metrics. The export prints how many were moved so the
count is visible rather than surprising.

## The NSE broker (Kite Connect)

`venues --venue nse` prints the seams and, at the end, whether there is a broker behind
them:

```bash
python -m tradebot venues --venue nse
#   NSE broker: kite - implemented, no keys: set KITE_API_KEY and KITE_ACCESS_TOKEN
KITE_API_KEY=... KITE_ACCESS_TOKEN=... python -m tradebot venues --venue nse
#   NSE broker: kite - reachable          (or the exact error, usually a token expiry)
```

With keys set, that probe calls `/user/margins`. A Kite access token expires daily, so
`unreachable: TokenException` at 09:00 is the normal Monday morning problem, not a code
fault.

The adapter refuses, locally and before any HTTP call: a quantity that is not a lot
multiple, a price off the tick, an order outside the session and a symbol without
instrument rules. It also looks for its own `tag` before placing anything, so a retry
after a timeout cannot double a position. That is tested; a live broker is not.

## Historical backfill

Before any strategy result means anything the archive needs real history:

```bash
python -m tradebot backfill --days 30 --timeframe 1m --write   # real exchange, needs network
python -m tradebot backfill --source demo --days 1 --write     # offline: proves the pagination
```

Read three numbers per symbol: `coverage` (kept ÷ expected bars), `gaps` (holes with their
sizes) and the verdict - `complete`, `incomplete`, or `STUCK`. `STUCK` means the endpoint
returned the same page twice and the fetch stopped instead of looping forever; `incomplete`
means some pages failed. Neither is silently hidden, and neither is filled with invented
bars: a backfill with holes is a backfill with holes.

The current bar is always dropped. A bar that has not closed yet is a future leak, and the
archive is the one place that must never contain one.

## Streaming feed

`--feed stream` runs the bot off a WebSocket instead of REST polling. Two commands matter:

```bash
python -m tradebot stream --minutes 120          # replay a recording through the feed
python -m tradebot stream --live --seconds 30    # try the real socket (needs network)
```

The replay path sends klines, depth snapshots, trades, one malformed frame and one
dropped connection through the production feed, and prints the verdict. You want:

```
  events   bars  151  books  15  ticks   48  decode errors 1
  connection attempts 2  connects 2  error reconnects 0  gap-filled bars 5
  verdict: stream contract holds
```

`decode errors 1` is the deliberately corrupted frame; `gap-filled bars` proves the
outage was noticed and backfilled. A replay that prints `gaps_filled 0` means the feed
is hiding an outage - stop and fix it before going near real money.

In this sandbox `--live` reaches the socket and is refused (`ConnectionResetError`), then
retries with backoff. That is the honest result: nothing here has ever decoded a frame
from a real exchange.

## Telegram commands

The listener starts with the bot (`tradebot run`) when `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` are set; there is no separate process. Check the wiring before trusting
it:

```bash
python -m tradebot telegram            # token present? chat id? does Telegram answer?
```

Expected when configured correctly:

```
token: ok   chat id: 123456789   telegram reachable: True
  pending updates: 0 (offset starts at 0, so a backlog is normal on the first check)
```

Exit codes: `0` reachable, `2` not configured (no token, or no allowed chat id), `3`
Telegram did not answer. A missing chat id is worth an exit code of its own: with a token
but no whitelist, every command is refused and the bot looks broken rather than locked.

Commands: `/status /pnl /positions /report /pause /resume /flatten /kill /help`.
`/flatten`, `/kill` and `/lockdown` require `/confirm` within 120 seconds, and the
confirmation is single-use. Anything from a chat id that is not whitelisted is ignored,
never answered, and written to the event log.

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

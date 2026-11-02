# Architecture

## The one rule

**Strategy, risk, sizing, execution and ledger code is identical in backtest, paper
and live.** Only two things are swapped:

| Concern | Backtest | Paper | Testnet | Live |
| --- | --- | --- | --- | --- |
| Feed | `ReplayFeed` / `SyntheticFeed` + `SimClock` | `SyntheticFeed` or `CcxtFeed` + `Clock` | `CcxtFeed` | `CcxtFeed` |
| Adapter | `PaperAdapter` | `PaperAdapter` | `CcxtLiveAdapter(testnet=True)` | `CcxtLiveAdapter` |
| Database | `:memory:` | `tradebot_paper.db` | `tradebot_testnet.db` | `tradebot_live.db` |

If a result cannot be reproduced by the paper path, it is not a result.

## Processes

1. **bot** (`tradebot run`) — one asyncio process: feed → market data hub → strategies
   → risk → order manager → adapter → ledger → store. Sequential, single-threaded,
   deterministic. No shared mutable state between tasks other than the store.
2. **watchdog** (`python -m tradebot.watchdog.watchdog`) — separate process with its
   own SQLite handle. Watches the heartbeat key and the `KILL` file. On silence it
   escalates the persisted kill level to L2 (and, in live, flattens through its own
   exchange client).
3. **dashboard** (`tradebot dashboard`) — read-only FastAPI app over the same
   database, plus optional controls. Never trades by itself; in the demo it can also
   host the bot so a single command gives a working system.

## Event flow

```
feed.emit(kind, payload)  ──await──▶  asyncio.Queue(1_000)  ──▶  Engine.dispatch()
```

Awaiting the queue is the backpressure mechanism: the producer cannot outrun the
engine, so during a backtest the simulated clock advances exactly with the events
being processed (freshness checks, cooldowns and deadlines are measured on *market*
time, not wall time).

| Event | Handler | Effects |
| --- | --- | --- |
| `tick` | `_on_tick` | spike filter → mark price → settle resting paper orders → manage positions |
| `book` | `_on_book` | book store → limit/stop settlement → position management |
| `bar` | `_on_bar` | append 1m bar, derive 15m/1h, run strategies on *closed* bars, equity snapshot |
| `fill` | `_on_fill` | ledger → position lifecycle → protective stop → cooldowns/streaks/tax reserve |
| `regime` | `dispatch` | recorded in the event log |

Position management on every price update: trailing stop (never down), stop hit,
target hit, partial take-profit at 1.5R, time stop at N bars.

## Signals and orders

A strategy returns a `Signal` — symbol, side, stop, target, confidence, reason text,
and the indicators behind it. It never sees balances or the exchange.

```
signal → RiskContext snapshot → RiskEngine.approve_entry (16 checks, returns qty)
       → OrderManager.submit_entry (idempotent client id) → adapter.place_order
       → fill event → Ledger.on_fill → store
```

`OrderRequest.make_client_id()` hashes `strategy|signal|symbol|side|type|tag`:

* a **retry** of the same intent reuses the id → the adapter returns the existing
  order instead of creating a second one;
* a **separate** intent on the same signal (a partial exit, then the target exit)
  carries a different `tag` → a different id.

## Research and validation pipeline

Nothing above changes when a strategy is validated; the validators are separate and read
the same engine through the same store.

```
              ┌──────────────── backtest/ ────────────────┐
  ReplayFeed ─┤ runner ─ metrics ─ gates                 │  single run
  SyntheticFeed┤ walkforward (purged folds)              │  rolling OOS
              │ sensitivity (grid + plateau shape)       │  parameter stability
              │ robustness (bootstrap, 2x cost rerun,    │  is it real
              │   benchmark, attribution, trial log)     │
              │ parity (record → replay → diff)          │  is paper = live
              └───────────────────────────────────────────┘

              ┌──────────────── ai/ ──────────────────────┐
  bars/books ─┤ features (32, causal, rolling-normalised)│
              │ signals  (44 in 8 families)               │
              │ screen   (IC on non-overlapping samples,   │
              │   stability, cost-adjusted PnL, corr prune)│
              │ ladder   (baseline → ridge → boosting)    │
              │ registry (model cards; approval enforced) │
              └───────────────┬───────────────────────────┘
                              │ only an approved card
                       strategies/ml_signal.py (S4) ──▶ Signal (still risk-sized)

              ┌──────────────── venues/ ──────────────────┐
              │ calendar · instruments · costs · session  │  NSE seams
              │ square-off scheduler                      │
              └───────────────────────────────────────────┘
```

Two boundaries are enforced by tests rather than convention:

* `risk/` never imports `ai/` - a model cannot reach the risk engine.
* `ai/` never imports `execution/`, `engine/` or `risk/` - a model cannot place an order
  even if it wanted to. It can only produce a score, which a strategy may turn into a
  candidate Signal that the risk engine then vetoes or sizes.

## Persistence

SQLite (WAL) holds everything that must survive a restart:

| Table | Purpose |
| --- | --- |
| `events` | append-only audit log: signals, verdicts, orders, fills, kill switches, incidents |
| `orders`, `fills` | execution records keyed by client order id |
| `positions` | open rows (`closed_at IS NULL`) and closed rows with denormalised PnL/R |
| `equity` | equity snapshots used for drawdown, Sharpe and the dashboard curve |
| `kill_events`, `incidents` | kill-switch history and incident summaries |
| `kv` | kill state, day/week start equity, peak equity, streak counters, ramp stage, heartbeat |
| `experiments`, `holdout_uses` | experiment registry (every sensitivity cell, ladder level and screen run) and the holdout lock |

On startup: kill level is loaded from the store (never cleared by a restart), open
positions are adopted, and reconciliation runs before anything trades.

## Why this shape

* **Strategy purity** makes replay and testing trivial: a strategy is data in, signal out.
* **Risk in one place** means every entry passes the same sixteen checks, in the same
  order, no matter who asked.
* **The ledger is the only source of PnL.** Adapters report cash; the ledger reports
  positions and realized PnL; reconciliation compares them and any gap is an incident.
* **Everything is recorded.** If you cannot explain a decision later from the event
  log, it did not happen correctly.

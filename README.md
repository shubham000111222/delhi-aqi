# tradebot

A paper-first, live-ready crypto trading system: real-time multi-coin market data,
rule-based strategies, a 17-check risk engine with a four-level kill switch, a
paper exchange that is deliberately pessimistic, an audit-grade ledger, daily
reports with tax records, and a local dashboard.

The point of the project is **engineering correctness, not a promise of profit**.
Every number it prints comes from a recorded run; strategies that fail validation
are reported as failures (see [Results](#results-so-far)).

```
                       ┌──────────────────────────────────────────────┐
   synthetic feed ─────▶│  MarketData hub   candles · books · staleness │
   or ccxt (REST)      └──────────────┬───────────────────────────────┘
                                      │ bar close (15m / 1h)
                       ┌──────────────▼───────────────┐
                       │  Strategies S1 · S2 · S3      │  pure functions of data
                       └──────────────┬───────────────┘
                                      │ signal (stop, target, reason)
                       ┌──────────────▼───────────────┐
                       │  Risk engine (17 checks)      │  veto + size
                       └──────────────┬───────────────┘
                       ┌──────────────▼───────────────┐
                       │  Order manager                │  idempotent ids, safe retries
                       └──────────────┬───────────────┘
              ┌───────────────────────┴───────────────────────┐
     ┌────────▼─────────┐                          ┌─────────▼─────────┐
     │  Paper exchange  │  book-walk fills, fees,  │  Live / testnet   │
     │  (default)       │  latency, rejects        │  adapter (ccxt)   │
     └────────┬─────────┘                          └─────────┬─────────┘
              └──────────────────┬─────────────────────────────┘
                       ┌─────────▼──────────┐        ┌───────────────────────┐
                       │ Ledger (Decimal)   │───────▶│ SQLite: state + events │
                       └─────────┬──────────┘        └───────────┬───────────┘
                                 │                                │
                     ┌───────────▼──────────┐        ┌────────────▼───────────┐
                     │ Daily reports + tax  │        │ Dashboard (FastAPI)     │
                     └──────────────────────┘        └────────────────────────┘
                       Kill switch L0–L3 ◀── watchdog process ◀── heartbeats
```

**Where the project stands and what is left:** [`docs/CHECKLIST.md`](docs/CHECKLIST.md) —
phase by phase, with the command and the pass criterion for each. The self-healing and
self-sustaining sections (§20–21) have their own status sheet:
[`docs/BUILD_PLAN_20_21.md`](docs/BUILD_PLAN_20_21.md).

## Quick start (no keys, no network, no real money)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

python -m tradebot check                  # validate config, print effective settings
python -m tradebot dashboard --speed 600   # bot + dashboard on http://localhost:8000
python -m tradebot backtest --days 10      # full engine run over synthetic data + gates
python -m tradebot drill                   # kill-switch fire drill, measures time-to-flat
python -m tradebot metrics                 # metrics + validation gates for the paper DB
python -m tradebot status                  # positions, kill switch, recent events
python -m tradebot dashboard-png           # render the dashboard to reports/dashboard.png
python -m tradebot learn                   # learn from the bot's own closed trades
```

Or through the Makefile: `make dashboard`, `make backtest`, `make learn`, `make soak`,
`make test`, `make lint`. `make soak` runs one supervised paper hour.

Everything runs on a **seeded synthetic market** by default: five coins, regime
shifts, order books with depth, spread and volume. Nothing leaves your machine and
no API key is needed.

## What is implemented

| Area | Status | Where |
| --- | --- | --- |
| Recorded-tape replay (`--feed archive`) | done (`depth` is modelled, and says so) | `data/archive.py`, `data/feed.py` |
| Real-time multi-coin market data (5 coins, 1m base, 15m/1h derived) | done | `data/feed.py`, `data/hub.py` |
| Paper exchange: book-walk fills, 0.1% fees, 100–500 ms latency, rejects, injected API failures | done | `execution/adapters/paper.py` |
| Idempotent client order ids, query-before-retry, safe-error-only retries, rate limiting | done | `execution/order_manager.py` |
| Risk engine: 16 pre-trade checks, sizing caps, exposure and bucket limits | done | `risk/engine.py`, `portfolio/sizing.py` |
| Kill switch L0–L3, persisted in SQLite, 10 automatic triggers, KILL file, separate watchdog | done | `risk/killswitch.py`, `watchdog/` |
| Loss handling: stops, time stop, 4-loss halving, 6-loss pause, daily/weekly/DD limits, cooldowns | done | `risk/`, `engine.py` |
| Profit handling: 50% at 1.5R, break-even, 3-ATR chandelier trail, daily profit locks | done | `engine.py` |
| Decimal ledger with average entry, fees, realized/unrealized PnL and invariants | done | `portfolio/ledger.py` |
| Reconciliation against the venue every health tick | done | `engine.reconcile` |
| Self-healing playbook: six verified repair rows (stale feed, API pressure + 60 s breaker, stuck exit, unprotected position, disk, reconcile), cooldowns, one escalation per burst, MTTR vs target | done | `heal.py`, `engine._build_playbook` |
| Daily report (11 sections) → markdown, HTML, JSON, Telegram text, trades CSV, tax CSV | done | `reporting/daily.py` |
| Tax reserve (30%) and TDS tracking for Schedule VDA records | done | `portfolio/ledger.py`, `reporting/` |
| Dashboard: equity curve, positions, risk usage, signals with reasons, trades, execution cost, veto breakdown by risk check, event log, controls, live connection bar | done | `dashboard/` |
| Dashboard PNG export for environments where a page will not render | done | `dashboard/render.py` |
| Backtest through the *same* engine, metrics, Monte Carlo, acceptance gates | done | `backtest/` |
| Live-mode guards: two switches, typed confirmation, go/no-go record, key permissions, ramp | done (adapter path untested without keys) | `risk/live_guards.py` |
| Live ccxt adapter | implemented, **not exercised here** (no exchange access from this sandbox) | `execution/adapters/ccxt_live.py` |
| Walk-forward validation with purged, embargoed folds | done | `backtest/walkforward.py` |
| Parameter sensitivity grid with plateau / isolated-peak verdicts | done | `backtest/sensitivity.py` |
| Backtest-vs-paper parity test on a recorded tape | done (`MATCH`, 22/22 decisions, 8/8 fills) | `backtest/parity.py`, `data/tape.py` |
| Robustness suite: block bootstrap, full cost-stress rerun, buy-and-hold benchmark, attribution | done | `backtest/robustness.py` |
| Multiple-testing log and deflated Sharpe (with skew/kurtosis correction) | done | `robustness.py`, `core/store.py` (`experiments`) |
| MLflow export of the experiment log (file store, verified by MLflow itself) | done | `ai/tracking.py` |
| MLflow **live** tracking: stream a running session to a tracking server, fail fast when it is down | done (verified against a real `mlflow server`) | `ai/tracking.py` |
| Learning from the bot's **own trades**: net-R labels, purged walk-forward, a card only on repeated evidence, and cards that expire | done, and it promotes nothing on this data (92 trades → "not yet") | `ai/ledger.py`, `tradebot learn` |
| Signal library: 44 signals in 8 families, IC screening, correlation pruning, leakage tests | done | `ai/features.py`, `ai/signals.py`, `ai/ladder.py` |
| Complexity ladder (baseline → ridge → boosting → sequence) with a promotion rule | done, and it **promotes nothing** on this data | `ai/ladder.py` |
| Model registry + model cards; S4 wired to an approved card | done, **no model is registered** | `ai/registry.py`, `strategies/ml_signal.py` |
| Slippage recorder, calibrated model, budget-capped live entries | done (median 0.0, p95 +26 bps measured) | `execution/slippage.py`, `engine.py` |
| Historical backfill: paginated REST candles, de-duplicated, gap-checked, archived | done (offline exchange; live endpoint unreachable here) | `data/backfill.py` |
| WebSocket streaming feed: injected transport, Binance decoders, backoff, REST gap fill | done (`stream` replay verdict, live socket blocked here) | `data/stream.py` |
| Meta-labelling: triple-barrier labels, logistic gate, promote/skip rule | done, and it **rejects every gate** on this data (|edge| ≈ 1–3 bps vs 20 bps costs) | `ai/meta.py` |
| Meta gate wired into the engine as a veto-only filter (`run --meta-gate`) | done, and **no card passes the save rule**, so nothing is installed by default | `meta_gate.py`, `ai/meta.py` |
| Reduce-only sentiment overlay (lexicon + optional FinBERT, RSS) | done, **no live news feed wired** | `ai/sentiment.py` |
| Parquet/DuckDB archive for candles and order books | done | `data/archive.py` |
| Telegram command bot with whitelist and confirm step | done | `alerts/commands.py` |
| Telegram transport: long polling with offset, sends, backoff, started by `run` | done (mock-tested; the real API is unreachable here) | `alerts/telegram.py` |
| NSE-ready seams: calendar, instruments, cost model, sessions, square-off | done | `venues/` |
| NSE execution: Kite Connect adapter, local rule checks, tag idempotency | done (mock-tested; no broker session here) | `execution/adapters/nse.py` |
| Live crypto exchange connectivity, a real broker session, LLM journal in production | **not built** — see `docs/ROADMAP.md` | — |

## One code path

Backtest, paper and live share the strategy, risk, sizing, execution and ledger
code. Only two things change: the **feed** and the **adapter**.

```python
engine = await Engine.build(config)          # feed=synthetic, adapter=paper
engine = await Engine.build(config, clock=SimClock(start), store=Store(":memory:"))  # backtest
```

That is what makes the paper results meaningful: they come from the same order
path, the same risk vetoes and the same ledger as a live run.

## Safety model (short version)

* **Fail closed.** If a live gate cannot be *verified*, it counts as failed.
* **Risk first, then exposure caps.** Size = risk budget ÷ stop distance, then every
  cap is applied and the smallest wins; the binding constraint is recorded.
* **Stop anything that looks wrong.** The kill switch escalates automatically. It comes
  back down narrowly and on the record: an **L1** caused by a *transient
  infrastructure* fault (stale feed, error-rate storm, reconciliation mismatch, disk)
  lifts only after ten minutes of verified health, and the **daily** loss limit lifts at
  the next trading day because its baseline is re-based. Everything else — the weekly
  limit, a drawdown, a manual pause, L2 and L3 — needs a human. L3 also needs a written
  reason and a one-hour cooldown. Every automatic change is written to the event log.
* **Never trust memory.** Kill level, positions, equity and the audit chain live in
  SQLite; a restart adopts open positions instead of forgetting them.
* **No secrets in logs.** Keys come from `.env` only, and a log filter redacts them.

See `docs/RISK_MODEL.md` for the full limit table and `docs/RUNBOOK.md` for what to
do at 3 a.m.

## Results so far

Honest, small, and from synthetic data. Ten simulated days, five coins, default
config, `python -m tradebot backtest --days 10`:

```
closed trades 97 | win rate 38.1% | fees 27.69 USDT | end equity 971.23 (start 1000)
validation gates:
FAIL  out_of_sample_profit_factor  PF 0.91 >= 1.3
FAIL  sharpe                       sharpe -9.77 >= 1.0
PASS  max_drawdown                 max DD -7.63% <= 15%
FAIL  trade_count                  97 trades >= 200
PASS  monte_carlo_drawdown         MC p95 DD -8.02% <= 20%
verdict: NOT PROMOTABLE
```

That verdict is the system working as designed. S1–S3 are untuned reference
implementations and the synthetic market is not a forecast of anything: the gates
exist so that nobody ships a strategy because it "looks okay". Turnover alone
(≈2.8% of equity in fees over ten days) is enough to kill an edge at 15-minute
signals, which is exactly the cost problem section 17.2 of the plan warns about.

### The validation suite, on the same data

Every one of these ran and every one of them agrees with the verdict above. They are
reported here because a failing strategy that is *proved* to fail is the deliverable. All
numbers are from **2026-11-03, commit `8486021`**, on the seeded synthetic market;
`docs/RESULTS.md` has every command and the caveats.

| Check | Result |
| --- | --- |
| Walk-forward (3 windows × 1 day, seed 5) | Windows +0.43%, −2.04%, +0.21% → compound **−1.42%**, "inconsistent"; OOS PF 0.20, Sharpe −11.4, 19 trades → **NOT PROMOTABLE** |
| Parameter sensitivity (S1 × `ema_fast` 12/20/30, 3 days) | Best cell +2.39%, neighbour median +2.35%, plateau share 1.0 → "broad plateau" — then the deflation kills it: best Sharpe 0.02 against the 1.48 expected from 3 attempts |
| Parity (recorded tape, 1 200 minutes) | **MATCH**: 22/22 decisions, 8/8 fills, 24 984 tape events |
| Robustness (4 days, 2× costs rerun) | Nominal **−1.29%** (PF 0.79, 27 trades), at 2× costs −2.25% (PF 0.68) → cost stress **FAILS**, "the edge is the fee"; 77% of gross P&L went to fees, and it trailed buy-and-hold by 0.93 points |
| Benchmark | Buy-and-hold −0.36% over the same bars; the strategy **trailed it by 1.13 points** |
| Signal screen (44 signals, 1 day) | Kept BTC 0/44, ETH 1/44, SOL 3/44, BNB 0/44, XRP 3/44; the IC noise band at ~46 non-overlapping samples is ±0.29, so most survivors are "thin", not proven |
| Complexity ladder (4 rungs) | Baseline retained everywhere; **ridge, boosting and the window model are all rejected** — and the window model is the worst (BTC: −2.5 / −2.3 / −0.6 / **−7.2** bps a bar at 2-3× the turnover) |
| Meta-labelling (1 day) | SOL AUC 0.485, XRP 0.531 → "do not use it": the gate's gain does not cover 20 bps of cost |
| Slippage (2 days) | p95 up to **+36 bps** per symbol (SOL), +26 (XRP), +16 (ETH); calibration error +19 bps, i.e. the model is *pessimistic*, not optimistic |
| Research loop (2 days, 6 run → 3 kept) | best cell +1.87%, deflated Sharpe 1.41, probability 0.92 against a 0.95 bar → **NO** ("weak: does not survive its own attempts") |
| Sentiment overlay (local RSS, 4 negative headlines) | 8 polls, 4 headlines kept, 28 duplicates dropped, 7 entries halved to ×0.50 at score −0.38 |
| Cost budget (target ≥ N× round-trip cost, 10 days) | N=4 and 8 inert (97 trades, PF 0.91); N=16 leaves 49 trades at PF 1.14 and Sharpe −4.15; N=32 falls back to PF 0.58 — noise on top of a loss, and nothing passes |

That is what "the AI layer" looks like when it is not allowed to lie: forty-four
candidate signals, a screen that throws most of them away, a gradient-boosted model that
loses to a weighted average once costs are charged, and no model card committed. S4
therefore returns no signals — and says why.

## Layout

```
config/                settings.yaml · risk.yaml · strategies.yaml · symbols.yaml
src/tradebot/
  core/                money (Decimal), models, clock, store (SQLite), bus, logging
  data/                feed (synthetic / ccxt / replay / websocket), hub, series, tape, archive
  strategies/          base, ema_trend, meanrev, breakout, regime, indicators
  portfolio/           ledger, sizing
  risk/                engine (17 checks, incl. the cost budget), killswitch, live_guards
  execution/           order_manager, rate_limit, slippage, adapters/{base,paper,ccxt_live,nse}
  venues/              calendar, instruments, costs, session, square-off (NSE seams)
  backtest/            metrics, runner, walkforward, sensitivity, parity, robustness
  reporting/           daily report, tax ledger rows
  ai/                  journal, features (32), signals (44), ladder, registry, meta, sentiment
  alerts/              notifier (outbound + dedupe), commands (router), telegram (transport)
  watchdog/            watchdog process
  dashboard/           FastAPI app + single-page UI
  engine.py main.py
tests/                 unit (core, data, execution, risk) + integration (engine, dashboard)
docs/                  architecture, risk model, runbook, go/no-go, roadmap, decisions
```

## Commands

| Command | What it does |
| --- | --- |
| `tradebot check` | validate all config; print effective settings and secret presence |
| `tradebot dashboard` | run the demo bot + dashboard (`--no-bot` for read-only) |
| `tradebot dashboard-png` | render the live dashboard to a PNG, no browser needed (`--out`, `--url`) |
| `tradebot backtest --days N` | deterministic synthetic run through the real engine, writes reports |
| `tradebot metrics` | metrics, Monte Carlo and acceptance gates for the paper database |
| `tradebot run` | paper trading on the configured feed until Ctrl-C or `--minutes N` (`--mlflow DIR` logs the session on exit, `--mlflow-server URI` streams it live, `--news-url` adds the reduce-only sentiment overlay, `--meta-gate` adds the meta veto filter, `--ledger-gate` adds one trained on the bot's own trades) |
| `tradebot status` | positions, kill level, recent events |
| `tradebot health` | one health check: per-component `HEALTHY`/`DEGRADED`/`FAILED` with the failing question (exit 1 if any critical component is `FAILED`), plus the heal playbook's attempts, verified count, p50 MTTR and target per row |
| `tradebot report` | build today's report now |
| `tradebot pause` / `resume` / `kill` / `lockdown` | manual kill-switch controls |
| `tradebot drill` | fire drill: flatten and measure time-to-flat |
| `tradebot walk-forward --train-days N --test-days M --windows K` | rolling train/test windows, out-of-sample gates |
| `tradebot sensitivity --strategy S1 --param ema_fast=12,20,30` | parameter grid with plateau / spike verdicts, every cell logged |
| `tradebot parity --minutes N` | record a session, replay it, diff decisions and fills |
| `tradebot robustness --days N --cost-multiplier 2` | bootstrap, full 2x-cost rerun, benchmark, attribution |
| `tradebot signals --days N --horizon H` | feature store → 44-signal screen → complexity ladder → leakage tests |
| `tradebot slippage --days N` | measured slippage distribution and a calibrated model |
| `tradebot meta --days N` | triple-barrier meta-labels, gate threshold search, promote/skip verdict |
| `tradebot stream --minutes N` | replay a recorded session through the streaming feed and print its verdict |
| `tradebot backfill --days N --write` | paginate historical candles into the archive and report coverage and gaps |
| `tradebot backfill-history --months 6` | fill months of history in one go: month-sized chunks, each written as it arrives, resumable from `data/backfill-history.json`, exit 4 on any incomplete chunk |
| `tradebot backtest --feed archive --days N` | replay the archive through the real engine (see *Replaying a recording*) |
| `tradebot research` | bounded successive-halving search; every attempt is logged and the winner is deflated, so it can answer "no" |
| `tradebot news --url FEED` | score an RSS feed and print the size multiplier it implies (`--file` for an offline copy) |
| `tradebot experiments --export DIR` | list the experiment log and write it as an MLflow file store |
| `tradebot experiments --read DIR` | read an MLflow file store back and print what is in it |
| `tradebot experiments --read-server URI` | read runs back from a tracking server (`http://`, `sqlite://`, `file://`) |
| `tradebot learn` | label the bot's own closed trades, walk forward over them, and write a veto card only if it earned one |
| `tradebot learn --inspect PATH` | say whether a saved card is still valid: age, threshold, training size (exit 3 when inert) |
| `tradebot backtest --persist PATH` | run the backtest into a real database so `learn`/`metrics`/`report` can read it |
| `tradebot run --mlflow-server URI` | stream the session's metrics to a running `mlflow server` while it runs |
| `tradebot telegram` | check the inbound-command wiring: token, chat id, does Telegram answer |
| `tradebot venues --venue nse --notional 100000` | session state, square-off deadline, round-trip cost comparison |
| `tradebot archive --record-minutes N` | persist a session to Parquet and query it with DuckDB |

`--mode paper|testnet|live`, `--feed synthetic|ccxt|stream|archive`, `--archive-root PATH`,
`--speed N` and `--config-dir` are accepted before or after the sub-command.

### Replaying a recording

`backfill` writes real candles to Parquet; `--feed archive` feeds them back through the
same engine, so `backtest`, `walk-forward`, `sensitivity`, `robustness` and `signals`
can all run on recorded prices instead of a seeded simulation:

```powershell
python -m tradebot backfill --days 30 --write            # record (needs exchange access)
python -m tradebot backtest --feed archive --days 30     # replay it
python -m tradebot walk-forward --feed archive --train-days 10 --test-days 5 --windows 3
```

A replay starts on the archive's first bar and stops at its last — a run asking for more
days than were recorded is clipped, not extended into empty time. The archive holds
candles, not depth, so a replay **models** a top-of-book around each bar close and prints
that caveat with its result: the spread is an assumption, not a measurement. Calibrate it
with `tradebot slippage` after a live session. `--feed ccxt`/`stream` with a simulated
clock is refused outright, because that combination opens a real socket during a backtest.

## Going live (read this first)

Live mode is refused unless *all* of these hold: `mode: live` in config **and**
`LIVE_TRADING_ENABLED=true` in the environment, a typed confirmation of the capital
amount plus the word `LIVE`, a go/no-go record less than 14 days old, verifiable API
key permissions with withdrawals disabled, a capital cap, clean preflight, and a
ramp stage that starts at 10% size. See `docs/GO_NO_GO.md`.

`ccxt_live.py` needs your own keys and network access, and has not been exercised
in this repository's sandbox (no exchange connectivity). Treat it as unproven until
you have run it against testnet.

## Licence

MIT. Educational project — not investment advice, no signals offered.

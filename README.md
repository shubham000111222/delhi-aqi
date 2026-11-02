# tradebot

A paper-first, live-ready crypto trading system: real-time multi-coin market data,
rule-based strategies, a 16-check risk engine with a four-level kill switch, a
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
                       │  Risk engine (16 checks)      │  veto + size
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
```

Everything runs on a **seeded synthetic market** by default: five coins, regime
shifts, order books with depth, spread and volume. Nothing leaves your machine and
no API key is needed.

## What is implemented

| Area | Status | Where |
| --- | --- | --- |
| Real-time multi-coin market data (5 coins, 1m base, 15m/1h derived) | done | `data/feed.py`, `data/hub.py` |
| Paper exchange: book-walk fills, 0.1% fees, 100–500 ms latency, rejects, injected API failures | done | `execution/adapters/paper.py` |
| Idempotent client order ids, query-before-retry, safe-error-only retries, rate limiting | done | `execution/order_manager.py` |
| Risk engine: 16 pre-trade checks, sizing caps, exposure and bucket limits | done | `risk/engine.py`, `portfolio/sizing.py` |
| Kill switch L0–L3, persisted in SQLite, 10 automatic triggers, KILL file, separate watchdog | done | `risk/killswitch.py`, `watchdog/` |
| Loss handling: stops, time stop, 4-loss halving, 6-loss pause, daily/weekly/DD limits, cooldowns | done | `risk/`, `engine.py` |
| Profit handling: 50% at 1.5R, break-even, 3-ATR chandelier trail, daily profit locks | done | `engine.py` |
| Decimal ledger with average entry, fees, realized/unrealized PnL and invariants | done | `portfolio/ledger.py` |
| Reconciliation against the venue every health tick | done | `engine.reconcile` |
| Daily report (11 sections) → markdown, HTML, JSON, Telegram text, trades CSV, tax CSV | done | `reporting/daily.py` |
| Tax reserve (30%) and TDS tracking for Schedule VDA records | done | `portfolio/ledger.py`, `reporting/` |
| Dashboard: equity curve, positions, risk usage, signals with reasons, trades, event log, controls | done | `dashboard/` |
| Backtest through the *same* engine, metrics, Monte Carlo, acceptance gates | done | `backtest/` |
| Live-mode guards: two switches, typed confirmation, go/no-go record, key permissions, ramp | done (adapter path untested without keys) | `risk/live_guards.py` |
| Live ccxt adapter | implemented, **not exercised here** (no exchange access from this sandbox) | `execution/adapters/ccxt_live.py` |
| ML signal (S4), FinBERT sentiment (S5), walk-forward CV, Telegram command bot | **not built** — deliberately, see `docs/ROADMAP.md` | — |

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
* **Stop anything that looks wrong.** The kill switch escalates automatically and only
  a human can reset it. L3 needs a written reason and a one-hour cooldown.
* **Never trust memory.** Kill level, positions, equity and the audit chain live in
  SQLite; a restart adopts open positions instead of forgetting them.
* **No secrets in logs.** Keys come from `.env` only, and a log filter redacts them.

See `docs/RISK_MODEL.md` for the full limit table and `docs/RUNBOOK.md` for what to
do at 3 a.m.

## Results so far

Honest, small, and from synthetic data. Ten simulated days, five coins, default
config, `python -m tradebot backtest --days 10`:

```
closed trades 57 | win rate 36.8% | fees 16.35 USDT | end equity 987.77 (start 1000)
validation gates:
FAIL  out_of_sample_profit_factor  PF 0.96 >= 1.3
FAIL  sharpe                       sharpe -8.33 >= 1.0
PASS  max_drawdown                 max DD -6.18% <= 15%
FAIL  trade_count                  57 trades >= 200
PASS  monte_carlo_drawdown         MC p95 DD -7.01% <= 20%
verdict: NOT PROMOTABLE
```

That verdict is the system working as designed. S1–S3 are untuned reference
implementations and the synthetic market is not a forecast of anything: the gates
exist so that nobody ships a strategy because it "looks okay". Turnover alone
(≈1.6% of equity in fees over ten days) is enough to kill an edge at 15-minute
signals, which is exactly the cost problem section 17.2 of the plan warns about.

## Layout

```
config/                settings.yaml · risk.yaml · strategies.yaml · symbols.yaml
src/tradebot/
  core/                money (Decimal), models, clock, store (SQLite), bus, logging
  data/                feed (synthetic / ccxt / replay), hub, series
  strategies/          base, ema_trend, meanrev, breakout, regime, indicators
  portfolio/           ledger, sizing
  risk/                engine (16 checks), killswitch, live_guards
  execution/           order_manager, rate_limit, adapters/{base,paper,ccxt_live}
  backtest/            metrics, runner (metrics + Monte Carlo + gates)
  reporting/           daily report, tax ledger rows
  ai/                  journal (template + optional grounded LLM)
  alerts/              notifier (Telegram + dedupe)
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
| `tradebot backtest --days N` | deterministic synthetic run through the real engine, writes reports |
| `tradebot metrics` | metrics, Monte Carlo and acceptance gates for the paper database |
| `tradebot run` | paper trading on the configured feed until Ctrl-C |
| `tradebot status` | positions, kill level, recent events |
| `tradebot report` | build today's report now |
| `tradebot pause` / `resume` / `kill` / `lockdown` | manual kill-switch controls |
| `tradebot drill` | fire drill: flatten and measure time-to-flat |

`--mode paper|testnet|live`, `--feed synthetic|ccxt`, `--speed N` and `--config-dir`
are accepted before or after the sub-command.

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

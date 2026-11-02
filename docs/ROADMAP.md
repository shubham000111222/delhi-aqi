# Roadmap: what is built, what is not, and why

The build plan is a ten-week programme. This repository implements the spine of it
(sections 2–7, 10–12) plus backtest metrics and gates. Everything below is deliberately
_out_ of scope for this pass, with the reason. Nothing here is silently stubbed: a
missing capability is marked missing.

## Built and tested

| Plan section | Implementation |
| --- | --- |
| 2 Architecture | single asyncio process, separate watchdog and dashboard, SQLite state + event log |
| 3 Modes and paper→live switch | paper/testnet/live, separate DBs and keys, seven live gates, enforced ramp |
| 4 Data layer | 1m base bars, derived 15m/1h, spike quarantine, staleness guards, gap detector, self-healing reconnect policy for the synthetic feed |
| 5 Strategies | S1 EMA trend (ADX), S2 mean reversion (BB+RSI), S3 Donchian breakout, regime detector |
| 6 Execution & paper exchange | idempotent ids, safe retries, rate limiter, book-walk fills, latency, rejects, injected failures |
| 7 Risk, kill switch, loss/profit handling | 16 checks, L0–L3, cooldowns, streaks, partials, trailing, locks, tax reserve |
| 8 Backtesting (engine + metrics + gates) | same-engine replay, Sharpe/Sortino/Calmar/DD/PF/expectancy, Monte Carlo, acceptance gates, post-tax view |
| 10 Reporting | daily report (11 sections) → markdown/HTML/JSON/Telegram/trades CSV/tax CSV |
| 11 Dashboard | live equity, positions, risk usage, signals with reasons, trades, event log, controls |
| 12 Reliability & security | crash recovery (positions adopted by the venue simulator), reconciliation, structured logs with redaction, heartbeats, disk guards, scoped auto-resume |
| 13 Testing | 109 tests: unit (core, data, execution, risk) + integration (engine, dashboard, reports) |

## Not built (and what it would take)

| Missing | Why it is not here | Next step |
| --- | --- | --- |
| **ML signal S4 / meta-labelling (plan §19)** | Needs years of real data, purged walk-forward CV and a model registry before it can be trusted; a stub that emits signals would be worse than nothing | store real 1m history, build the feature set, add `xgboost`, log runs to the `experiments` table |
| **Sentiment overlay S5 (FinBERT)** | Requires a model download and a news feed; also the easiest place to import prompt-injection or garbage data | offline FinBERT scoring job, headline source with dedupe, score as a size modifier only |
| **Parquet + DuckDB candle/order-book store (§4, §18.1)** | The synthetic demo needs no archive, and adding it without real data would be untested code | `data/store.py` writing partitioned Parquet, retention job, DuckDB views |
| **Historical backfill via REST (§4)** | This sandbox has no exchange connectivity; untestable code that touches money paths is a liability | paginate `fetch_ohlcv`, resumable, validated against the live candle builder |
| **WebSocket streaming** | Free `ccxt` has no WS; `ccxt.pro` is a paid package | add a `ccxt.pro` or native-WS feed implementing the same `BaseFeed` |
| **Telegram command bot (interactive)** | Outbound notifications are implemented; inbound commands need a bot loop and a whitelist store | long-polling loop calling `Engine.pause/kill/flatten/resume`, confirm step for destructive commands |
| **Walk-forward + parameter sensitivity harness (§8)** | Metrics and gates exist; the windowed runner does not | loop of train/test windows calling `Engine` with `ReplayFeed` |
| **Parity test (§8)** | Needs one recorded live day | record feed events, replay, diff decisions and fills |
| **LLM journal (live call)** | The grounded template path is implemented and number-checked; the API call is written but switched off without keys | set `LLM_PROVIDER`/`LLM_API_KEY`, verify the number cross-check on real output |
| **NSE/SEBI abstractions (§18.8)** | Same engine is intended to be reused; the seams are named in the plan | `MarketCalendar`, NSE `CostModel`, `Instrument`, `SessionManager` |
| **MLflow experiment backend** | Schema and holdout lock exist in SQLite; no MLflow dependency | wire `save_experiment` to MLflow's file backend |

## Known limitations

* **The strategies do not pass the acceptance gates** on synthetic data (see README
  results). They are reference implementations, not a strategy pack.
* **15-minute signals are fee-sensitive.** ≈1.6% of equity went to fees over ten
  simulated days. Any real deployment needs either lower turnover or maker-only entries.
* **The synthetic market is not a forecast.** It has regime shifts and correlated
  moves, which is enough to exercise the plumbing, not enough to validate an edge.
* **Live adapter is unproven.** `ccxt_live.py` is written against the same interface
  but has not been run against an exchange from this repository.
* **The watchdog can only escalate in paper mode.** Flattening requires its own
  exchange client, which exists as `LiveWatchdogActions` but needs live keys.
* **Time stops, trailing stops and partials are managed locally** in paper; in live the
  protective stop is also placed exchange-side, and the local monitor cancels it before
  exiting so the two cannot both fire.

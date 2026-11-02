# Roadmap: what is built, what is not, and why

The build plan is a ten-week programme. This repository implements the spine of it
(sections 2–8, 10–12, most of 18 and 19). Anything still missing is listed at the bottom
with the reason. Nothing is silently stubbed: a missing capability is marked missing, and
a capability that exists but has not cleared its gates is marked as such.

## Built and tested

| Plan section | Implementation |
| --- | --- |
| 2 Architecture | single asyncio process, separate watchdog and dashboard, SQLite state + event log |
| 3 Modes and paper→live switch | paper/testnet/live, separate DBs and keys, seven live gates, enforced ramp |
| 4 Data layer | 1m base bars, derived 15m/1h, spike quarantine, staleness guards, gap detector, self-healing reconnect policy for the synthetic feed |
| 4 / 18.1 Archive | partitioned Parquet candles and order books, DuckDB views, idempotent rewrites, retention job, `spread_stats()` from recorded books |
| 5 Strategies | S1 EMA trend (ADX), S2 mean reversion (BB+RSI), S3 Donchian breakout, regime detector |
| 6 Execution & paper exchange | idempotent ids, safe retries, rate limiter, book-walk fills, latency, rejects, injected failures |
| 6 Slippage | per-fill measurement against the *decision* price, size-vs-depth buckets, calibrated model, budget-constrained sizing |
| 7 Risk, kill switch, loss/profit handling | 16 checks, L0–L3, cooldowns, streaks, partials, trailing, locks, tax reserve |
| 8 Backtesting (engine + metrics + gates) | same-engine replay, Sharpe/Sortino/Calmar/DD/PF/expectancy, Monte Carlo, acceptance gates, post-tax view |
| 8 Walk-forward | rolling train/test windows with purged, embargoed folds; per-window and pooled OOS results |
| 8 Sensitivity | parameter grids, plateau share, neighbour isolation, and a sign check so a wide plateau of losses is not "approved" |
| 8 Robustness | block bootstrap of returns, trade-order bootstrap, **full rerun at 2× costs**, buy-and-hold benchmark, attribution by strategy/symbol/exit/hour, "does profit survive removing the best trade" |
| 8 Multiple testing | every grid cell logged as an experiment; best Sharpe deflated by the number of attempts |
| 8 Parity | record the tape, replay it, diff decisions and fills (`MATCH`, 22/22 and 8/8) |
| 10 Reporting | daily report (11 sections) → markdown/HTML/JSON/Telegram/trades CSV/tax CSV |
| 11 Dashboard | live equity, positions, risk usage, signals with reasons, trades, event log, controls, PNG export |
| 12 Reliability & security | crash recovery, reconciliation, structured logs with redaction, heartbeats, disk guards, scoped auto-resume |
| 12 Inbound commands | Telegram router: chat-id whitelist, confirm step with TTL and single use, audit line per command, transport-agnostic |
| 13 Testing | 287 tests: unit (core, data, execution, risk, venues, ai) + integration (engine, dashboard, reports, parity) |
| 18.1 Book recording | recorded on every session (`RecordingFeed`), archived to Parquet, summarised as spread statistics |
| 18.6 Experiment tracking | `experiments` table, deterministic run ids, trial log used by the multiple-testing report |
| 18.8 NSE-ready seams | `MarketCalendar` + `NSECalendar`, `Instrument` (tick/lot/band), `NSECostModel` (STT, GST, stamp, DP), daily-expiring sessions, square-off scheduler, engine hooks |
| 19.1 Signal library | 32 features in 7 families, 44 signals in 8 families, IC on non-overlapping samples, stability, cost-adjusted PnL, correlation pruning, shuffled-label check |
| 19.2 Complexity ladder | baseline → ridge → gradient boosting on purged folds; promotion needs +10% net expectancy in every window, enforced in code |
| 19.7 Explainability / leakage | ridge weight shares with a >40% warning, multi-permutation shuffled-label test, far-shift test, Platt calibration with Brier score |
| 19.8 Guardrails | risk package imports nothing from `ai`; the AI layer imports no execution/engine code (both enforced by tests); unapproved or stale models produce no trade |
| 19.9 What stays private | no model card is committed and `models/*.json` is gitignored |

## Not built (and what it would take)

| Missing | Why it is not here | Next step |
| --- | --- | --- |
| **Any strategy that passes the gates** | The strategies are reference implementations, the data is synthetic, and costs eat an untuned 15-minute signal. Three independent validation methods agree | real recorded data first; then lower turnover or maker-only entries; rerun the whole suite |
| **Sentiment overlay S5 (FinBERT)** | Needs a model download and a news feed, and is the easiest place to import prompt-injection or garbage data | offline FinBERT scoring job, headline source with dedupe, score as a size modifier only |
| **Neural / sequence models (ladder rung 4)** | The plan allows rung 4 only if it beats rung 3 net of costs; rung 3 has not earned promotion, so rung 4 has nothing to beat | not before rung 3 wins |
| **Live exchange connectivity** | This sandbox has no exchange access; untestable code on money paths is a liability | run `ccxt_live` against testnet with keys, then a 2-week paper soak |
| **WebSocket streaming** | Free `ccxt` has no WS; `ccxt.pro` is paid | add a `ccxt.pro` or native-WS feed implementing the same `BaseFeed` |
| **Historical backfill via REST** | Same connectivity problem; a half-tested paginator is worse than none | paginate `fetch_ohlcv`, resumable, validated against the live candle builder |
| **Live Telegram transport** | The router and confirm flow are tested against a fake transport; the long-poll client needs a token and network | implement `get_updates`/`send_message` over `httpx`, point it at the router |
| **NSE execution adapter** | The seams exist (calendar, costs, instruments, sessions, square-off), but no Indian broker API is implemented | Kite/Zerodha adapter behind the same `ExchangeAdapter` interface; verify tariff constants against current circulars |
| **MLflow experiment backend** | Schema and holdout lock exist in SQLite; no MLflow dependency | wire `save_experiment` to MLflow's file backend |
| **Autonomous research loop (19.5)** | An automatic search over strategies multiplies the multiple-testing problem this repo already takes seriously | only after the trial log and promotion rule have been used by hand for a while |

## Known limitations

* **The strategies do not pass the acceptance gates** on synthetic data (see README
  results). They are reference implementations, not a strategy pack.
* **15-minute signals are fee-sensitive.** ≈2.8% of equity went to fees over ten
  simulated days. Any real deployment needs either lower turnover or maker-only entries.
* **The synthetic market is not a forecast.** It has regime shifts and correlated
  moves, which is enough to exercise the plumbing, not enough to validate an edge.
* **The signal screen is sample-starved.** One simulated day gives ~46 non-overlapping
  observations at a 30-bar horizon, so the IC noise band is ±0.29. Signals labelled
  "thin" are not evidence of anything and the tool says so.
* **The boosting rung is a reference implementation**, not LightGBM: depth-limited
  stumps with shrinkage and early stopping. It exists to answer "does non-linearity help
  here?" and so far the answer is no.
* **Slippage is measured on a synthetic book.** The p95 of +26 bps reflects this venue
  simulator's depth, not Binance's.
* **Live adapter is unproven.** `ccxt_live.py` is written against the same interface
  but has not been run against an exchange from this repository.
* **The watchdog can only escalate in paper mode.** Flattening requires its own
  exchange client, which exists as `LiveWatchdogActions` but needs live keys.
* **NSE cost constants must be verified.** They are constructor arguments, not gospel:
  brokerage caps, STT rates and stamp duty change with circulars.
* **Time stops, trailing stops and partials are managed locally** in paper; in live the
  protective stop is also placed exchange-side, and the local monitor cancels it before
  exiting so the two cannot both fire.

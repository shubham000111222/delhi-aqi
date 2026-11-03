# Roadmap: what is built, what is not, and why

The build plan is a ten-week programme. This repository implements the spine of it
(sections 2–8, 10–12, 18 and 19). Anything still missing is listed at the bottom
with the reason. Nothing is silently stubbed: a missing capability is marked missing, and
a capability that exists but has not cleared its gates is marked as such.

## Built and tested

| Plan section | Implementation |
| --- | --- |
| 2 Architecture | single asyncio process, separate watchdog and dashboard, SQLite state + event log |
| 3 Modes and paper→live switch | paper/testnet/live, separate DBs and keys, seven live gates, enforced ramp |
| 4 Data layer | 1m base bars, derived 15m/1h, spike quarantine, staleness guards, gap detector, self-healing reconnect policy |
| 4 Historical backfill | resumable `fetch_ohlcv` pagination, per-bar de-duplication, unclosed bar dropped, gap detection with sizes, coverage and `stuck` reporting, writes straight into the Parquet archive, `tradebot backfill` |
| 4 Streaming feed | `WebSocketFeed`: injected transport, Binance frame decoding (closed klines, depth snapshots, trades), symbol canonicalisation, exponential backoff with jitter, REST gap fill on reconnect, health counters, `--feed stream` and a `stream` replay command |
| 4 / 18.1 Archive | partitioned Parquet candles and order books, DuckDB views, idempotent rewrites, retention job, `spread_stats()` from recorded books |
| 5 Strategies | S1 EMA trend (ADX), S2 mean reversion (BB+RSI), S3 Donchian breakout, regime detector |
| 6 Execution & paper exchange | idempotent ids, safe retries, rate limiter, book-walk fills, latency, rejects, injected failures |
| 6 Slippage | per-fill measurement against the *decision* price, size-vs-depth buckets, calibrated model, budget-constrained sizing, and a live entry cap at `execution.max_slippage_bps` once 30 fills have calibrated the model |
| 7 Risk, kill switch, loss/profit handling | 17 checks (including the cost budget: target vs round-trip cost), L0–L3, cooldowns, streaks, partials, trailing, locks, tax reserve |
| 8 Backtesting (engine + metrics + gates) | same-engine replay, Sharpe/Sortino/Calmar/DD/PF/expectancy, Monte Carlo, acceptance gates, post-tax view |
| 8 Walk-forward | rolling train/test windows with purged, embargoed folds; per-window and pooled OOS results |
| 8 Sensitivity | parameter grids, plateau share, neighbour isolation, and a sign check so a wide plateau of losses is not "approved" |
| 8 Robustness | block bootstrap of returns, trade-order bootstrap, **full rerun at 2× costs**, buy-and-hold benchmark, attribution by strategy/symbol/exit/hour, "does profit survive removing the best trade" |
| 8 Multiple testing | every grid cell logged as an experiment; best Sharpe deflated by the number of attempts, with the skew/kurtosis correction and a probability alongside the z-score |
| 8 Parity | record the tape, replay it, diff decisions and fills (`MATCH`, 22/22 and 8/8) |
| 10 Reporting | daily report (11 sections) → markdown/HTML/JSON/Telegram/trades CSV/tax CSV |
| 11 Dashboard | live equity, positions, risk usage, signals with reasons, trades, execution-cost panel (median/mean/p95 vs budget), a veto breakdown by failing check, event log, controls, PNG export |
| 12 Reliability & security | crash recovery, reconciliation, structured logs with redaction, heartbeats, disk guards, scoped auto-resume |
| 12 Inbound commands | Telegram router: chat-id whitelist, confirm step with TTL and single use, audit line per command, transport-agnostic |
| 12 Telegram transport | `getUpdates` long polling with persisted offset, `sendMessage`, backoff on failure, injected HTTP client, started by `run` when a token and chat id exist, `tradebot telegram` readiness check |
| 13 Testing | 582 passing, 2 skipped (MLflow is a dev dependency, so CI exercises the tracking paths): unit (core, data, execution, risk, venues, ai, streaming) + integration (engine, dashboard, reports, parity, ledger) |
| 18.1 Book recording | recorded on every session (`RecordingFeed`), archived to Parquet, summarised as spread statistics |
| 18.6 Experiment tracking | `experiments` table, deterministic run ids, trial log used by the multiple-testing report |
| 18.6 MLflow export | `experiments --export DIR` writes MLflow's file-store layout, `run --mlflow DIR` writes each session as it ends, and `experiments --read DIR` reads a store back into the bot; non-numeric metrics move to tags; verified by reading all three back with real MLflow 3.17 |
| 18.6 MLflow live tracking | `run --mlflow-server URI` streams points to a running tracking server while the session runs (http, sqlite or file store), terminates the run at the end, and `experiments --read-server URI` reads it back; MLflow is an optional dependency, a dead server is caught by a 3-second TCP pre-check and disables tracking instead of delaying the session; verified end to end against a real `mlflow server` |
| 18.8 NSE execution | `KiteAdapter`: order shaping (LIMIT/MARKET/SL/SL-M, MIS/CNC/NRML), local tick/lot/band checks before any HTTP call, tag-based idempotency with a lookup after a timeout, status mapping, balances, positions, LTP cache, closed-session refusal, `venues --venue nse` reports readiness |
| 18.8 NSE-ready seams | `MarketCalendar` + `NSECalendar`, `Instrument` (tick/lot/band), `NSECostModel` (STT, GST, stamp, DP), daily-expiring sessions, square-off scheduler, engine hooks |
| 19.1 Signal library | 32 features in 7 families, 44 signals in 8 families, IC on non-overlapping samples, stability, cost-adjusted PnL, correlation pruning, shuffled-label check |
| 19.5 Research loop | bounded successive halving over a declared space: every attempt is a logged trial, the survivor is deflated by the attempts that produced it, the verdict is allowed to be "no", and it never registers a model or edits config |
| 19.2 Complexity ladder | baseline → ridge → boosting → sequence (a numpy MLP over a 4-row window) on purged folds; promotion needs +10% net expectancy in every window, enforced in code |
| 19.7 Explainability / leakage | ridge weight shares with a >40% warning, multi-permutation shuffled-label test, far-shift test, Platt calibration with Brier score |
| 19.3 Ledger learning | `tradebot learn` labels the bot's own closed trades (net R, fees included), runs a purged walk-forward over them, and writes a card only on repeated, cost-stressed evidence; `run --ledger-gate` installs it as a veto-only entry filter. On this data the honest answer is "not yet" (92 trades) or "no edge" |
| 19.3 Meta-label runtime gate | `MetaEntryFilter` installed as the engine's veto-only entry filter (`run --meta-gate card.json`): a saved card is loaded, features are rebuilt from the live bar window and cached per bar close, and anything unexpected lets the trade through with a logged reason |
| 19.8 Guardrails | risk package imports nothing from `ai`; the AI layer imports no execution/engine code (both enforced by tests); unapproved or stale models produce no trade |
| 19.9 What stays private | no model card is committed and `models/*.json` is gitignored |

## Not built (and what it would take)

| Missing | Why it is not here | Next step |
| --- | --- | --- |
| **Any strategy that passes the gates** | The strategies are reference implementations, the data is synthetic, and costs eat an untuned 15-minute signal. Three independent validation methods agree | real recorded data first; then lower turnover or maker-only entries; rerun the whole suite |
| **A live news feed and FinBERT weights** | The lexicon scorer, the optional FinBERT scorer, the RSS parser, the dedupe/decay store and the reduce-only sizing overlay all exist and are tested (28 tests), but nothing here fetches headlines and the FinBERT weights were never downloaded | point `overlay_from_provider` at a real feed, score offline with FinBERT, and keep the multiplier capped at 1.0 |
| **Live exchange connectivity** | Everything between the bot and the network is now tested against a fake ccxt exchange (27 tests: order shaping, idempotent client ids, retry classification, partial-fill deltas, permissions failing closed, book parsing). What no test here covers is ccxt's HTTP layer and the venue's real answers - and this sandbox has neither | run `ccxt_live` against testnet with keys, then a 2-week paper soak |
| **A WebSocket session against a real exchange** | The feed, its decoders and its reconnect/gap-fill logic are written and tested, but this sandbox blocks the socket (it was tried: `ConnectionResetError`, three reconnects, no frames) | run `python -m tradebot stream --live` from a networked host and watch `describe()` |
| **A backfill against a real exchange** | The paginator, its de-duplication and its gap report are tested against a fake endpoint and a deterministic offline exchange; `--source ccxt` cannot reach Binance from this sandbox | `python -m tradebot backfill --days 30 --write` from a networked host, then check `coverage` and `gaps` |
| **A Telegram session against the real API** | The transport is implemented and tested through an `httpx` mock, but this sandbox cannot open TLS to `api.telegram.org` (`ConnectError`, reported as `telegram reachable: False`) | run `python -m tradebot telegram` with a real token |
| **An NSE order at a real broker** | `KiteAdapter` is written and tested through an `httpx` mock, but nothing here has a Kite session (the sandbox cannot reach `api.kite.trade`, and a pointing-at-prod test would be reckless) | run `venues --venue nse` with real keys (it probes margins), then one 1-share order in a test account; verify tariff constants against current circulars |

The full evidence sheet — every command, every number, dated — is in `docs/RESULTS.md`.

## Known limitations

* **The strategies do not pass the acceptance gates** on synthetic data (see README
  results). They are reference implementations, not a strategy pack.
* **15-minute signals are fee-sensitive.** ≈2.8% of equity went to fees over ten
  simulated days. Any real deployment needs either lower turnover or maker-only entries.
* **The research loop searches; it does not validate.** Its recommendation means "worth a
  walk-forward", never "promotable": a halving search on one window is still a search on
  one window, and on two simulated days it returns "no" because the deflation kills the
  winner (probability 0.92 against a 0.95 bar). Short stages also make its Sharpe noisier
  than a daily one - the loop annualises at the curve's own resolution when there are
  fewer than three daily returns, and says so in the module docstring.
* **The synthetic market is not a forecast.** It has regime shifts and correlated
  moves, which is enough to exercise the plumbing, not enough to validate an edge.
* **The signal screen is sample-starved.** One simulated day gives ~46 non-overlapping
  observations at a 30-bar horizon, so the IC noise band is ±0.29. Signals labelled
  "thin" are not evidence of anything and the tool says so.
* **The boosting rung is a reference implementation**, not LightGBM: depth-limited
  stumps with shrinkage and early stopping. It exists to answer "does non-linearity help
  here?" and so far the answer is no.
* **Rung 4 is a small numpy network, not a sequence model in the modern sense.** One
  hidden layer over the last four rows, Adam, early stopping: 2,209 parameters and no
  recurrence, attention or embeddings. It is enough to test whether temporal structure
  adds net expectancy - on two simulated days it loses 3 to 8 bps a bar and trades 2-3x
  as often as the simpler rungs, so the answer is again no. A real sequence model would
  need recorded data and a GPU budget, and would still have to clear the same margin.
* **Sentiment is feed-dependent, and the lexicon is weak.** The overlay can only shrink a
  position, so a bad feed costs edge rather than money - but the lexicon scores headlines
  as bag-of-words, and no FinBERT weights were ever downloaded here (`FinBertScorer` is
  written and takes any `transformers` pipeline). The one thing not to read into it: on a
  mixed feed the aggregate score is a weighted mean, so one alarming story among cheerful
  ones can stay above the -0.15 cut and change nothing.
* **Slippage is measured on a synthetic book.** The p95 of +26 bps reflects this venue
  simulator's depth, not Binance's.
* **Live adapter is unproven.** `ccxt_live.py` is written against the same interface
  but has not been run against an exchange from this repository.
* **The stream feed has never held a real socket.** Every frame it has decoded came
  from a recording, and the live transport is the one line of it that no test here
  exercises end to end.
* **The watchdog can only escalate in paper mode.** Flattening requires its own
  exchange client, which exists as `LiveWatchdogActions` but needs live keys.
* **NSE cost constants must be verified.** They are constructor arguments, not gospel:
  brokerage caps, STT rates and stamp duty change with circulars.
* **Time stops, trailing stops and partials are managed locally** in paper; in live the
  protective stop is also placed exchange-side, and the local monitor cancels it before
  exiting so the two cannot both fire.

# Pre-trade Time Machine — phased implementation

User requirement recorded 2026-11-02. This is a new feature, not evidence that the
existing strategy is safe or profitable. Risk package must never import analysis/AI.

## Phase 5 — implemented foundations, not an enabled filter

- [x] `src/tradebot/analysis/timemachine.py`, outside risk.
- [x] Point-in-time 5m closed-bar selection, last 60 days by default, minimum 30.
- [x] Refuse missing/duplicate/stale bars, invalid OHLCV, NaNs and zero volatility.
- [x] Full-precision input hash, deterministic trade-ID seed and immutable standardized
  log-OHLC returns relative to previous close. Cross-check cache is not implemented yet.
- [x] Historical rule callback sees only a closed prefix; next-open market execution,
  long/short, two-leg fees, adverse entry/stop/time slippage, gap stop fills at open,
  no overlapping positions, bounded holding horizon, and censored-tail accounting.
- [x] Replay uses 60–180 days of supplied history (default 60), reports insufficient
  when fewer than 20 completed triggers, and returns net mean/median/p05 R and holding
  time with target/stop/time counts. No triggers means null metrics, not zero edge.
- [x] OHLC ambiguous touches are counted and stop-first. `first_touch_verified` remains
  false: OHLC does not establish actual intrabar ordering.
- [ ] Adapt each supported entry/stop/target strategy rule to this replay interface;
  current utility supports a market-next-open plan, not limit-order/queue simulation.

## Phase 6 — pending, filter stays disabled

- [ ] Cached standardized returns keyed by symbol, as-of window, input hash and settings.
- [x] Standalone vectorized 10,000-path volatility-scaled block bootstrap (blocks 6–12 bars);
  deterministic seed, sampled intrabar path assumptions, fees/slippage/gap exits.
- [ ] Define and validate horizon and current-volatility estimator without final-holdout
  tuning. Sampled intrabar ordering is modeled uncertainty, never known first touch.
- [x] Standalone simulation output: target/stop/time probabilities, net expected/median/p05 R, expected holding
  time and input/seed/path provenance. Side-by-side orchestration remains pending.
- [x] Standalone risk-reducing verdict function: PASS 1.0 when E[R] >= .10 and replay insufficient or
  nonnegative; SIZE DOWN .5 for 0 < E[R] < .10 or sign disagreement; SKIP 0 for E[R] <= 0,
  p05 < -1.5R, or time-exit probability > .60 with E[R] < .05. SKIP conditions take
  precedence over sign disagreement. |E[R]| < .05 is labelled coin flip with configured
  size-down/skip (never overriding a mandatory SKIP). Thresholds validation-only.
- [ ] All missing/invalid/error/timeout handling skips and alerts. No risk bypass; apply
  multiplier before the existing risk engine, never above base size.
- [ ] Enforce measured 250 ms end-to-end check budget; repeated overruns reduce paths
  to 5,000 and alert. No timing target has yet been measured or claimed.
- [ ] Run no-filter, simulation-only, replay-only and combined variants inside the
  backtester with decision-time data only, identical costs/capital and untouched final
  holdout. Retain only if OOS expectancy improves or max DD falls while keeping at least
  50% of baseline trades; otherwise disable. No fabricated real OOS evidence.
- [x] Core tests for Monte Carlo determinism, symmetric random-walk probabilities, adverse costs and verdict multiplier bounds.
- [ ] Complete end-to-end prefix-invariance, failure-skip alerts and enforced latency tests.

## Phase 8 — pending integration

- [ ] Persist every check: trade ID, seed, paths, lookback, hash, probabilities, net mean
  and p05 R, replay statistics, verdict, multiplier, timing and error/skip reason.
- [ ] Dashboard Trades page verdict and daily-report list of skipped/sized-down trades.
- [ ] Plain-word order ticket from computed results only; no illustrative numbers
  presented as actual runs. Alert delivery for invalid inputs/errors/path degradation.
- [ ] End-to-end backtest and engine tests prove the risk engine still runs and the
  filter cannot increase size, resume trading, clear holds or loosen limits.

## Honest limits

Only recent data is sampled; news, halts, outages and regime changes cannot be foreseen.
Simulation and replay share data, so agreement is weaker evidence than independent
confirmation. OHLC cannot prove first-touch order; sampled intrabar paths are assumptions.
Odds are a sanity check, never a profit promise. Foundation tests are synthetic fixtures,
not venue/OOS certification. No strategy validation or live execution is enabled here.

Core-only timing observed in the sandbox: 10,000 paths, wide barriers, 48-bar horizon
56.11 ms; 288-bar horizon 257.58 ms. These are single measurements, not end-to-end
latency acceptance or a performance guarantee. The 250 ms gate remains unfulfilled.

# Pre-trade Time Machine — phased implementation

User requirement recorded 2026-11-02. This is a new feature, not evidence that the
existing strategy is safe or profitable. Risk package must never import analysis/AI.

## Phase 5 — implemented foundations, not an enabled filter

- [x] `src/tradebot/analysis/timemachine.py`, outside risk.
- [x] Point-in-time 5m closed-bar selection, last 60 days by default, minimum 30.
- [x] Refuse missing/duplicate/stale bars, invalid OHLCV, NaNs and zero volatility.
- [x] Full-precision input hash, deterministic trade-ID seed and immutable standardized
  log-OHLC returns relative to previous close; bounded cross-check cache implemented.
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

- [x] Bounded process-local standardized-return/history cache keyed by symbol, decision time and full closed-bar values; replay cache additionally binds costs, callback and explicit immutable rule version.
- [x] Standalone vectorized 10,000-path volatility-scaled block bootstrap (blocks 6–12 bars);
  deterministic seed, sampled intrabar path assumptions, fees/slippage/gap exits.
- [ ] Define and validate horizon and current-volatility estimator without final-holdout
  tuning. Sampled intrabar ordering is modeled uncertainty, never known first touch.
- [x] Standalone simulation output: target/stop/time probabilities, net expected/median/p05 R, expected holding
  time and input/seed/path provenance; side-by-side orchestration implemented.
- [x] Standalone risk-reducing verdict function: PASS 1.0 when E[R] >= .10 and replay insufficient or
  nonnegative; SIZE DOWN .5 for 0 < E[R] < .10 or sign disagreement; SKIP 0 for E[R] <= 0,
  p05 < -1.5R, or time-exit probability > .60 with E[R] < .05. SKIP conditions take
  precedence over sign disagreement. |E[R]| < .05 is labelled coin flip with configured
  size-down/skip (never overriding a mandatory SKIP). Thresholds validation-only.
- [x] Standalone orchestrator catches missing/invalid/error/timeout outcomes, skips and invokes a required alert callback; alert delivery failure is recorded.
- [ ] Wire real alert delivery and risk-boundary integration. No risk bypass; apply
  multiplier before the existing risk engine, never above base size.
- [x] Cooperative deadline checks in replay/simulation and before result acceptance; three consecutive overruns reduce subsequent checks to 5,000 paths and invoke alerts.
- [ ] Prove <=250 ms end-to-end latency on supported histories/horizons, including cache misses and production alert delivery. Cooperative checks do not preempt slow callbacks or hashing/preparation; those can exceed the deadline before returning SKIP.
- [x] Standalone four-run study driver in `backtest/timemachine_study.py`: declared holdout exclusion, identical input/settings/capital checks, net-expectancy/drawdown comparison and >=50% baseline trade retention. Never enables the filter by itself.
- [ ] Run no-filter, simulation-only, replay-only and combined variants inside the
  backtester with decision-time data only, identical costs/capital and untouched final
  holdout. Retain only if OOS expectancy improves or max DD falls while keeping at least
  50% of baseline trades; otherwise disable. No fabricated real OOS evidence.
- [x] Core tests for Monte Carlo determinism, symmetric random-walk probabilities, adverse costs and verdict multiplier bounds.
- [ ] Complete end-to-end prefix-invariance, failure-skip alerts and enforced latency tests.

## Phase 8 — pending integration

- [x] Persist every research-engine check in the event audit: trade ID, seed, paths, lookback, hash, probabilities, net mean
  and p05 R, replay statistics, verdict, multiplier, timing and error/skip reason.
- [x] Dashboard check/verdict panel beside Trades and daily-report list of skipped/sized-down candidates. Views disclose bounded history/truncation and distinguish candidate checks from actual fills.
- [x] Plain-word research order ticket from persisted computed results only; missing metrics explicitly unavailable, no illustrative odds.
- [ ] Verify external alert delivery for invalid inputs/errors/path degradation.
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

## Simulated engine bridge increment

- [x] Explicit opt-in paper/SimClock/PaperAdapter-only hook before risk approval; SKIP prevents approval and DOWN reduces existing size multiplier. Audit failure holds entries.
- [x] Real checker bridge for market entries, explicit recorded-history provider/versioned replay binding, actual candidate stop/target and persisted provenance. 24 integration cases; no implicit strategy or default activation.
- [ ] Implement and verify each actual strategy replay binding, archived history provider and independent engine study runner; validate OOS retention/benefit and timing. Trades dashboard and daily report integration remain open.

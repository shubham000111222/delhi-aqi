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

## Recorded history increment

- [x] `ArchiveHistoryProvider` loads only closed5m symbol bars in the as-of window with bounded row count, exact Decimal values and no synthetic/resample/fill fallback. Archive provenance remains caller-supplied/unverified.
- [ ] Validate supplied archive coverage/provenance on real data and include provider I/O in end-to-end latency acceptance; actual strategy-specific replay/limit-entry semantics and independent study wiring remain open.

## Existing strategy opinion replay increment

- [x] Versioned frozen settings, existing deterministic strategy/regime evaluation, complete5m-to-strategy-timeframe aggregation and market EMA plan parity. Fresh instances; no repeated signal from an incomplete latest bucket.
- [ ] Limit-fill/queue semantics for default strategies, stateful ML replay and full portfolio/exit-management parity remain unsupported. No silent market substitution. Optimize/measure cold replay before latency acceptance.

## Research ablation modes

- [x] Engine bridge/checker mode isolation: simulation-only bypasses replay, replay-only bypasses bootstrap (0 paths), combined remains default; baseline has no hook. Replay-only requires 20 completed triggers and applies the same fixed mean/tail/time/coin-flip thresholds to replay statistics; insufficient evidence skips. This is a predeclared research convention, not an enabled live rule.
- [ ] Execute actual isolated engine/store/portfolio runs per variant on identical point-in-time recorded inputs; callbacks/declarations alone are not proof. No final-holdout tuning.
- [ ] Resolve limit fills/queue position and 120-second timeout using adequate finer event data:5m bars cannot verify these semantics.

## Four-engine study implementation

- [x] Concrete independent Engine/ReplayFeed/PaperAdapter/store/clock/ledger/config runs, no injected result callback; actual frozen input/settings hashes and strategy+timeframe binding selection. Cold-start windows and modeled books/fills are explicit limitations.
- [x] Archive-backed research CLI, bounded parameterized pre-holdout queries, required checker cost assumptions, no-overwrite JSON evidence and no enablement path. Invalid/truncated checks suppress retention. Tests of zero-trade windows only establish wiring, never an edge.
- [ ] Real recorded OOS data, meaningful trades, limit-entry expiry/queue parity, calibrated costs and hard latency acceptance remain unverified.

### Windows11 / PowerShell research CLI

Use `python -m tradebot.backtest.timemachine_cli --help` from the repository with the virtual environment active. Define `$ArchiveRoot`, `$StudyStart`, `$StudyEnd`, `$HoldoutStart`, `$FeeRate` and `$SlippageRate` from your recorded dataset and documented, frozen research assumptions; timestamps must include a timezone. Checker fee/slippage rates are separate from the unchanged engine paper-execution settings; neither is verified venue cost evidence. Do not change the default limit strategies to market just to produce a passing study.

```powershell
python -m tradebot.backtest.timemachine_cli `
  --config-dir config --archive-root $ArchiveRoot `
  --start $StudyStart --end $StudyEnd --holdout-start $HoldoutStart `
  --fee-rate $FeeRate --slippage-rate $SlippageRate --cost-stress `
  --output reports/timemachine-study.json
```

The output refuses overwrite, always reports `filter_enabled=false` and `provenance_verified=false`, and includes retention arithmetic, input/settings hashes, per-run diagnostics and bounded candidate audit records. An incomplete study exits2. A successful command is research execution, not promotion authorization or a profit claim.

- [x] Study integrity: contiguous valid execution coverage for every configured symbol, aligned bounds, final-close multi-symbol drain and expected emitted/processed base-bar count. Incomplete/lockdown runs cannot pass retention.

- [x] Optional actual2x modeled-cost rerun across all four variants with fresh engines/portfolios, doubled paper fees/adverse market slippage and checker cost assumptions, unchanged strategy parameters and same input hash. `stress_survivors` additionally requires nominal retention and nonzero profitable stress trades without check failures. Still no verified venue costs or promotion authority.

## Predeclared multi-window research

- [x] `run_window_studies` and CLI `--windows-file` execute3–24 ordered non-overlapping pre-holdout windows, frozen settings with no refit/best-window selection. Every window must meet nominal retention and profitable modeled2x cost stress for the conservative research screen. Without `--cost-stress`, research screens cannot pass. Never a production gate or verified OOS proof.

The JSON manifest is a list of objects with aware ISO `start` and `end` strings, fixed before evaluation. It replaces `--start`/`--end`; do not edit the declared windows after examining outcomes to obtain a passing screen. Define `$WindowsFile` and the other variables from documented research inputs, then use PowerShell:

```powershell
python -m tradebot.backtest.timemachine_cli `
  --config-dir config --archive-root $ArchiveRoot `
  --windows-file $WindowsFile --holdout-start $HoldoutStart `
  --fee-rate $FeeRate --slippage-rate $SlippageRate --cost-stress `
  --output reports/timemachine-windows.json
```

Reports preserve independent window drawdowns, not a fabricated concatenated curve. Source-data provenance, real meaningful OOS trades, entry/expiry parity and latency still require acceptance. No filter is enabled by a research screen.

- [x] Terminal censoring: reports preserve marked open positions and unresolved order/fill/submission flags. A fully consumed feed may be complete but comparison-ineligible. Retention/stress screens require uncensored baseline and variant; no automatic liquidation at invented prices.

- [x] History-provider I/O is now included in the checker cooperative timer via lazy loading. Slow-provider returns skip before hashing/replay/bootstrap, provider exceptions use required alert callback, and repeated provider overruns degrade paths. Bridge validation, audit/notifier time and hard preemption still need end-to-end acceptance.

## Current-volatility estimator

- [x] Optional `--current-volatility` uses288 closed5m close-log returns (trailing24h, sample sigma/ddof1) relative to prepared history sigma. Zero/nonfinite estimates skip; future appends cannot change past ratios. Estimator revision, observations, cutoff and baseline/current sigma/scale are persisted with computed checks. Fixed-scale default remains1 unless the option is explicitly chosen.
- [ ] Validate this predeclared reference estimator and horizon on recorded non-holdout inputs; no automatic performance claim. Add `--current-volatility` to either research CLI example to freeze this option across nominal/stressed windows. Changing it after inspecting outcomes is parameter selection, not fresh OOS evidence.

- [x] Concrete study diagnostics include existing performance metrics, trade-P&L bootstrap MC (unavailable without trades), and unchanged project acceptance gates. Actual eligible same-input stressed return feeds cost-stress gate; OOS provenance remains false. Research retention/screen flags do not override acceptance gates or enable trading.

- [x] Study evidence includes bounded closed-trade records and truncation flag; nonzero artificial-market integration confirms ordinary strategy/risk/orders/fills/fees/closed-trade metrics and missing-history filter suppression. Test-only market settings do not alter defaults or certify limit/OOS execution.

- [x] Complete bounded metrics evidence: shared collector fetches one extra equity/trade record and refuses overflow; display pagination/truncation is not used as the metrics dataset. Streaming large-dataset evaluation remains future work.

- [x] Metrics evidence structural validation refuses missing/invalid monetary fields and identities, nonfinite exposure, naive/missing timestamps and backwards chronology; no current-time/zero repair of missing evidence. Does not attest venue truth.

- [x] Research evidence publication is strict JSON,64MiB bounded, file-fsynced and atomically no-clobber installed. Unsupported hard-link filesystems fail closed; partial/scratch files are not published evidence. Windows filesystem support, power-loss durability and directory permissions remain external acceptance items.

- [x] Evidence/settings identity includes bounded local Python-source digest and file manifest, checked after each engine and across nominal/stressed runs. Dependencies/interpreter, privileged mutation and venue/OOS provenance remain outside this fingerprint.

- [x] Engine-boundary verdict contract: PASS1/SIZE DOWN0.5/SKIP0 only; missing/contradictory verdicts, malformed flags, positive failed/timed-out results and enablement claims skip. Does not authenticate caller evidence or bypass risk.

- [x] `--replay-days` selects60–180-day replay cap (minimum60 valid contiguous days); bootstrap always uses last60 days/minimum30. Separate replay/bootstrapped hashes and cache keys prevent longer replay data entering bootstrap. Default60 unchanged; select lookback before evaluating outcomes, not to tune a holdout. Longer-window latency is unverified.

- [x] History requires UTC-aligned5m timestamps, not merely contiguous rows with a5m label; equal aware instants canonicalize to the same timestamp hash across timezone representations.

- [x] Insufficient replay is not permission to ignore malformed present metrics. NaN/Infinity/boolean/text expectancy skips; coin-flip setting is strictly boolean, costs/scales cannot be booleans masquerading as numbers.

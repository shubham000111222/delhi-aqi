# Results — measured, reproducible, and mostly negative

The historical results farther below were run on **2026-11-03** from commit `8486021` (plus the dashboard veto
panel) in this repository, with the default config, on the **seeded synthetic market**.
Nothing here is a claim about real crypto markets: the synthetic feed has regime shifts and
correlated moves, which is enough to exercise the plumbing and nowhere near enough to
establish an edge.

Reproduce any row with the command in it. If a number here disagrees with a rerun, the
rerun is right and this file is stale — fix it rather than the other way round.

## Complete bounded metrics evidence

- Shared backtest collector no longer silently calculates metrics from the last100,000 equity rows or closed trades. It fetches one extra row per table and raises EvidenceLimitError on overflow. Exact-limit inputs remain valid; caller-selected smaller bounds are validated. This affects metrics/backtest/walk-forward consumers as well as Time Machine, keeping promotion gates from using a truncated evidence tail.
- Display-only closed-trade/check record pages remain explicitly bounded/truncated; they are distinct from metrics input completeness. No thresholds are relaxed and no partial input is relabeled a complete study. Time Machine catches overflow as incomplete, disables retention and never enables the filter. Large studies require an explicit future streaming/pagination design, not a larger hidden cap.
- Nine unit cases cover exact-limit collection, equity/trade overflow, five invalid limits and empty input; one study failure-injection covers fail-closed propagation. Full suite **1421 passed, 2 skipped**; Ruff/mypy/diff clean. This is evidence-integrity hardening, not OOS/profit or latency certification.

## Time Machine nonzero executed-trade plumbing test

- Concrete study evidence now retains up to1,000 closed-trade records per variant (symbol/strategy, net P&L/R, fees, open/close timestamps and exit reason), with explicit trade_records_truncated disclosure. Metrics continue to use the collected trade set rather than just the displayed record page.
- Added an artificial1,800-minute two-symbol trend fixture that actually exercises the existing EMA strategy, ordinary risk engine, orders, fills, closed trades, fees and trade-P&L Monte Carlo. Baseline produces nonzero closed trades; all three filters reject the deliberately missing historical lookback and submit no orders. Invalid-check retention and project acceptance remain false.
- This fixture explicitly uses market entry/test-only settings and a disabled target-cost-ratio check solely to reach the plumbing under deterministic artificial prices. Repository defaults/risk configuration are unchanged. It is NOT strategy tuning, a recommendation to switch default limit entries to market, recorded OOS evidence, fill parity or a profitability result. The test verifies nonzero fees, bounded records, count consistency and in-window trade timestamps.
- Full suite **1411 passed, 2 skipped**, Ruff/mypy/diff clean. Recorded OOS benefit, full limit/expiry semantics and hard latency acceptance still unfulfilled; filter remains disabled.

## Time Machine existing project acceptance gates

- Concrete studies now compute ordinary project performance metrics and trade-P&L bootstrap drawdown per variant, separately from10,000-path candidate simulation. Initial capital/final marked equity are included explicitly in the metrics curve. No trades means unavailable Monte Carlo evidence (`null`), not a fabricated zero-drawdown pass.
- Reports include existing unchanged acceptance_gates: PF>1.3, Sharpe>1, DD<15%, >=200 trades, MCp95DD<20%, actual modeled2x-cost positive return and independently verified OOS provenance. Eligible same-input completed stressed runs supply their measured total return; missing/censored/invalid stress evidence cannot satisfy that gate. A separate comparison-integrity gate exposes unresolved/check-failure state.
- Acceptance results remain DISTINCT from filter-retention/research screens. OOS provenance always fails here; there is no automatic activation, relaxed threshold or assertion that research retention is deployment approval. Runner revision bumped in settings hash.
- New integration test verifies all expected zero-trade/provenance/MC/stress failures despite clean plumbing, with unchanged200-trade/PF1.3 thresholds. Full suite **1410 passed, 2 skipped**, Ruff/mypy/diff clean. No real OOS edge, live readiness or final-holdout evidence established.

## Time Machine current-volatility scaling

- Added fixed trailing24h estimator: sample standard deviation (ddof1) of the last288 closed5m close-log returns divided by the prepared history's sigma. Bootstrap OHLC is scaled by this ratio, preserving the joint sampled excursion model. No final-holdout fitting or future rows are used. Zero/nonfinite recent volatility refuses instead of inventing a scale.
- Explicit research option `volatility_scale=None`, exposed as CLI `--current-volatility`, propagates unchanged through engine bindings, nominal/stressed studies and multi-window runs. Existing explicit fixed-scale defaults remain unchanged; replay-only does not estimate an unused simulation scale. Settings hashes record which option was requested; checker audit records estimator revision,288 observations, window end, baseline/current sigma and computed ratio.
- Four new tests cover regime-sensitive scaling, exact future-append invariance, zero-vol refusal and checker provenance/argument forwarding. Full suite **1409 passed, 2 skipped**; Ruff/mypy/diff clean. This is a fixed reference estimator, not recorded-market calibration or OOS evidence; horizon selection, limit-entry parity, hard timing and live acceptance remain unfinished.

## Time Machine provider-inclusive cooperative deadline

- Engine bridge now supplies history lazily to the checker. The checker starts its clock before invoking the provider, catches provider errors inside the required alert/error boundary and tests the deadline immediately on provider return, before preparation/hash/replay/bootstrap.
- Slow reads therefore return SKIP with elapsed time including I/O; three consecutive provider overruns share the ordinary10,000-to5,000 path degradation/alert logic. Provider failures preserve candidate/seed/rule identity and alert delivery status in the engine audit rather than relying only on optional notifier handling.
- Two integration cases with injected clocks/providers verify300ms reads never call replay or simulation, three overruns degrade paths, and archive exceptions invoke the required callback and persist failure provenance. Full suite **1405 passed, 2 skipped**, Ruff/mypy/diff clean.
- This fixes deadline accounting, not hard preemption: blocking I/O, indicator computation and alert delivery cannot be interrupted at250ms. Bridge plan validation and engine audit/notifier handling remain outside the checker's timer. Real end-to-end latency acceptance remains unfulfilled; no production enablement.

## Time Machine terminal censoring and unresolved state

- Per-variant diagnostics now include terminal open positions (actual marks), pending entry count, venue/stored open order counts and durable pending-fill/submission flags. Any such unresolved state marks the variant terminal_censored and comparison-ineligible; no synthetic liquidation or fabricated final closed trade is applied.
- Nominal retention requires both baseline and filtered variant eligible. Actual2x modeled-cost stress survival additionally requires eligible stressed baseline and variant, preventing unrealized or unfinished execution from earning apparent stress profitability. `complete` still means all input bars were processed, not that all trade lifecycles completed; `comparison_eligible` distinguishes those facts explicitly.
- Two failure-injection tests verify an unresolved baseline blocks otherwise permissive comparison results, and marked open positions remain disclosed without incrementing closed-trade counts. Full suite **1403 passed, 2 skipped**; Ruff/mypy/diff clean. This conservative screen may reject legitimate boundary-open strategies; entry cutoff/liquidation policies require predeclaration and execution modeling, not after-the-fact tuning. Still no real OOS edge, venue parity or live enablement.

## Time Machine predeclared multi-window studies

- Added `backtest/timemachine_windows.py` and CLI `--windows-file` for 3–24 predeclared ordered, non-overlapping windows entirely before final holdout. Windows share frozen strategy/risk/settings and input containers; no refitting or best-window selection. Each window uses the concrete isolated engines and optional actual cost-stress rerun.
- Reports per-window evidence without inventing pooled drawdown across independently reset portfolios. Conservative `research_screen_passed` requires nominal retention and profitable modeled2x-cost survival in EVERY window. Missing stress evidence cannot pass; incomplete windows or changed settings fail closed. No automatic activation, provenance attestation or substitute promotion gate.
- CLI manifest is capped16KiB, refuses mixed single-window arguments, validates aware cutoffs before reading archive and retains bounded/no-overwrite evidence output. Eight new cases cover five invalid-window conditions, real three-window zero-trade engines, mocked all-window failure/settings checks and real manifest CLI execution. Full suite **1401 passed, 2 skipped**; Ruff/mypy/diff clean.
- All real-engine fixtures here are short plumbing windows without meaningful trades, NOT the required recorded OOS studies or approximately10% net edge. Cold starts, modeled books/fills, unsupported default limit entries and unverified venue costs/data remain limitations. All filters stay disabled and holdout_used remains false.

## Time Machine actual modeled-cost stress rerun

- Added optional `cost_stress=True` / research CLI `--cost-stress`: executes four additional fresh engines with doubled paper fees and adverse modeled market slippage via the existing stress_config implementation. Checker fee/slippage assumptions are doubled too; frozen strategy parameters are not refit. No post-hoc P&L subtraction or nominal ending-capital reuse.
- Nested evidence retains nominal/stressed results and distinct settings hashes with identical input hashes. `stress_survivors` requires nominal retention, completed stressed study, nonzero stressed trades, positive stressed ending profit and no invalid stressed checks. This is an additional research test, not all promotion gates; filter_enabled remains false. A failed/out-of-range stress model makes the study incomplete and cannot pass retention.
- Integration test observes eight fresh stores/engines and actual changes in paper/checker costs, source-config preservation, hash identity, disabled zero-trade survivors. Failure-injection covers the bounded slippage model; CLI test exercises stress JSON output/no overwrite. Full suite **1393 passed, 2 skipped**, Ruff/mypy/diff clean.
- These tests still use zero-trade plumbing fixtures, not real OOS evidence or cost calibration. Limit prices remain unchanged (no invented queue/expiry behavior); real venue costs and execution parity remain unverified. No profit, final-holdout benefit or deployment readiness claim.

## Time Machine study coverage and completion safety

- Research runner now requires aligned study boundaries and contiguous, valid OHLCV execution coverage for every configured symbol from start through the final close. Missing symbols, gaps/duplicates, NaNs and invalid prices refuse before building engines.
- Fixed the final-close boundary: prefiltered finite feeds now drain fully instead of stopping after the first symbol at the shared end timestamp. Every variant must emit AND process the expected base-candle count; incomplete runs and L3 lockdown return incomplete evidence with retention disabled. Two-symbol integration fixtures now assert all40 base bars are processed per variant.
- New failure-injection tests verify early exit cannot pass and missing-symbol/gap/NaN input never reaches Engine.build. Full suite **1391 passed, 2 skipped**; Ruff/mypy/diff passed. This is study integrity hardening, not real recorded-data OOS evidence, execution parity or enablement.

## Time Machine concrete four-engine runner and evidence CLI

- Added `backtest/timemachine_engine_study.py`, executing all four variants through actual Engine/ReplayFeed/PaperAdapter with fresh config, in-memory store, simulated clock, ledger and strategies each time. Temporary state/heartbeat/report paths isolate runs; no live mode. Inputs are frozen, cutoff-filtered and hashed internally, with unchanged config/cost assumptions across variants. Bindings now select by strategy AND timeframe to avoid applying a15m replay rule to a1h candidate.
- Computes closed-trade net R and marked equity comparisons, retains bounded candidate audit records in returned evidence, and suppresses retention when operational checker failures or incomplete check coverage occur. Explicit failure flags distinguish invalid checks from ordinary policy skips. No-filter has no hook; independent mode selection attaches only to the three filtered runs.
- Added `python -m tradebot.backtest.timemachine_cli`: parameterized bounded archive queries before study-end/holdout boundary, aware timestamp validation, explicit checker cost assumptions and no-overwrite JSON evidence output. No evidence file or run can enable trading. Provenance remains unverified.
- Six new cases test real four-engine state isolation, disabled zero-trade retention, future append hash invariance, holdout/live rejection before building engines, timeframe-specific binding precedence, and CLI evidence/no-overwrite/naive refusal (some cases exercise multiple assertions). Full suite **1389 passed, 2 skipped**, Ruff/mypy/diff clean.
- These integration windows are intentionally small plumbing fixtures with no meaningful trades: NOT OOS strategy evidence. Runs cold-start indicators at window start; books/fills are modeled by existing ReplayFeed/PaperAdapter, and engine-vs-checker costs are separate explicit assumptions. Default limit strategies remain unsupported by the filter, ML/stateful bindings remain refused, provider/checker budgets are not hard end-to-end timing guarantees. No venue provenance, limit queue/120-second expiry parity, real OOS advantage, production promotion or live readiness established.

## Time Machine research ablation modes

- Checker and simulated engine bridge now explicitly select `simulation_only`, `replay_only` or `both` (default unchanged). Simulation-only does not call the replay rule; replay-only does not run bootstrap paths and reports path_count=0. Omitted evidence is omitted, not fabricated as a computed result. Unknown modes skip and alert.
- Simulation-only applies existing simulation thresholds without replay sign disagreement. Replay-only requires >=20 consistent completed triggers and applies the same predeclared mean/tail/time/coin-flip thresholds to observed replay outcomes/proportions. Insufficient replay alone skips (unlike combined mode where valid simulation can stand alone). This conservative research convention is explicit, not supplied real OOS evidence or live policy. The no-filter baseline uses no candidate hook.
- End-of-check deadline/error handling explicitly overwrites any partially calculated permissive result with SKIP/0. Replay-only overruns never trigger a meaningless bootstrap path reduction. No hard-preemption/timing guarantee.
- Ten new tests cover exact component call isolation for three modes, five insufficient/invalid/unsafe replay cases, unknown-mode alerts and a permissive computed result arriving after deadline. Full suite **1383 passed, 2 skipped**; Ruff, mypy and diff clean. Actual four independent engine runs and recorded OOS comparison remain pending.
- Existing entry timeout is 120 seconds, shorter than 5m OHLC: current data cannot resolve limit-fill-before-expiry or queue position. Default limit entries remain unsupported, never substituted with market fills. Finer event data/model validation is required for honest parity.

## Time Machine deterministic strategy replay bindings

- Added `analysis/strategy_replay.py`: constructs contexts from complete aligned strategy-timeframe candles aggregated from the supplied closed 5m prefix. Rejects gaps/duplicates/mixed symbols/timeframes, drops incomplete edge buckets, bounds indicator history to 1,500 candles and evaluates only at a newly closed strategy bar.
- Reuses the existing EMA/mean-reversion/breakout implementations and regime detector, with fresh rule instances and deep-copied settings. Config/regime/timeframe/horizon/adapter revision determine a logged replay version hash. ML/custom stateful classes are refused; a frozen binding can be supplied to TimeMachineEngineGate.
- Market EMA parity is tested against direct existing-strategy evaluation for identical entry, stop and target. Limit-emitting strategies are explicitly rejected instead of silently converted to market. Default strategies predominantly use limits, so their supported execution replay is still incomplete. This replays strategy opinions, not full portfolio/managed-exit behavior, order books or venue execution; the context uses flat/no-book state and fixed reference equity for these deterministic rules only.
- Nine new tests cover exact EMA plan parity, complete aggregation/no repeated partial-tail signal, config snapshot/version changes, prefix stability, limit refusal, four corrupt input cases, unsupported class/timeframe. Full suite **1373 passed, 2 skipped**, Ruff/mypy/diff clean. No real-data evidence or timing acceptance. Aggregation/indicator recomputation is a bounded reference implementation, not optimized for a 60-day replay in 250 ms; cooperative timeout still skips.

## Time Machine recorded archive provider

- Added `analysis/archive_history.py` callable provider for the research engine bridge. Queries only the selected symbol's closed 5m rows at the decision cutoff, with 30–180-day bounded windows (default 60) and an extra-row overflow guard. Preserves Decimal prices, duplicates and gaps for independent checker validation; never resamples, forward-fills, deduplicates or falls back to synthetic input.
- Archive queries now support bound parameters; the provider binds symbol/window/limit values and view creation escapes apostrophes in archive paths. Future/unclosed rows are excluded by the database query before conversion. Existing archive query callers remain compatible.
- Nine new cases cover exact precision, symbol/timeframe/cutoff isolation, future appends/invalid future values, SQL-like symbol strings, quoted paths, missing archives, naive timestamps, lookback bounds, preserved duplicates/gaps and real engine/checker short-history rejection without archive writes.
- Full suite **1364 passed, 2 skipped**, Ruff/mypy/diff clean. Tests use locally written fixture Parquet, not independently verified venue data. Reading archive data does not attest provenance/completeness; checker still rejects insufficient/stale/corrupt history. File reads/query time are not inside the checker's cooperative timing budget; no end-to-end 250 ms acceptance claimed. No default activation or strategy replay/limit-entry certification.

## Time Machine reporting integration

- Added bounded read-only persisted-check views and plain-word order tickets with actual simulation/replay statistics. Missing/nonfinite metrics say unavailable. No illustrative or AI-generated odds. Candidate identity, verdict/multiplier, modeled-first-touch caveat and no-profit-promise text appear with the computed ticket.
- Dashboard snapshot and escaped check panel beside Trades show newest checks and disclose truncation; a skipped candidate is not mislabeled an executed trade. Daily JSON/Markdown/HTML include a day-bounded check page (up to 1,000); skipped/down lists are included in text renderers, with Telegram capped to 20 and an explicit full-report pointer. Reports disclose the cap rather than claiming exhaustive history.
- Added exclusive `until` event query boundary so later-day activity cannot crowd earlier-day checks out of the report. Seven new tests cover tickets, missing/NaN numbers, bounded/read-only/history-window behavior, API/escaped UI and report integration.
- Full suite **1355 passed, 2 skipped**; Ruff/mypy/diff passed. The full dataset and strategies remain synthetic/test-only; no real OOS advantage, production alert delivery, <=250 ms timing or live enablement claimed. Current strategies predominantly emit limit orders; market-only bootstrap/replay must not silently substitute for limit execution.

## Time Machine simulated-engine bridge

- Added explicit `Engine.set_research_candidate_gate` injection restricted at attachment and execution to paper mode, SimClock and PaperAdapter. Candidate checks precede risk approval; SKIP returns before sizing and DOWN multiplies the ordinary risk context size multiplier. No risk package imports analysis, no default/CLI/live enablement, and existing kill/session gates remain authoritative.
- Added `analysis/engine_gate.py`: supplies actual candidate identity, reference mark, side, stop, target, recorded-history provider, explicit versioned strategy replay binding, costs and horizon to the real TimeMachine checker. Unsupported limit entries, future candidates, missing bindings and provider errors skip through the engine. No generic replacement strategy is invented.
- Results are persisted in the event audit as `timemachine_check`, including checker provenance and input/settings fields. Identity mismatch, invalid/out-of-bounds multiplier, malformed/oversized results and checker errors skip; audit failure latches a local entry hold. Optional notifier exceptions also hold entries. This is event persistence, not the pending Trades UI/daily report integration or verified external alert delivery.
- Recreated and extended `tests/integration/test_timemachine_engine.py`: **24 integration cases**, including real 10,000-path bootstrap through the bridge with 60 days of synthetic bars and ignored invalid future data. That test freezes the budget clock to test wiring, not performance. Full suite **1348 passed, 2 skipped**, Ruff and mypy clean. Original missing file contents were not recoverable; these are new tests.
- Actual strategy-specific replay bindings, recorded-data providers, four independent engine study runs, real OOS benefit and hard latency acceptance remain unfinished. The research hook is not attached by default, including in the running dashboard. Live remains blocked.

## Time Machine four-variant research comparison (working branch)

- Added a backtest study driver invoking no-filter, simulation-only, replay-only and combined runner callbacks independently. It requires identical declared window, input/settings hashes and starting capital, unique run IDs and finite trade/equity results. Declared study windows overlapping the final holdout are rejected before invoking a runner.
- Computes net expectancy, maximum equity drawdown and trade retention. A filtered variant meets retention arithmetic only if expectancy improves or drawdown falls, with at least 50% of baseline trades and nonzero trades. Empty baseline, unchanged results, mismatching inputs or failed runs cannot pass. Filter remains disabled regardless; provenance_verified is always false.
- Full suite **1324 passed, 2 skipped**; Ruff, mypy and diff checks passed. Nine new synthetic-result tests cover comparison boundaries, empty/unchanged cases, holdout rejection and six invalid-run conditions. An earlier full run exposed a cold-cache timing dependency in the mocked overrun test; injected test clocks now isolate control-flow assertions from performance, without weakening actual deadlines.
- This is a callback-driven comparison framework, not completed engine integration or real OOS evidence. A caller can misdeclare hashes/windows; unique run IDs do not prove fresh portfolio/store isolation. Production strategy replay adapters, decision-time backtester wiring, real recorded study data, final-holdout governance and measured end-to-end latency remain open. No advantage or live readiness is claimed; filter stays disabled.

## Time Machine bounded orchestration pass (working branch)

- Added standalone TimeMachine.check with bounded full-value point-in-time history/standardized-return and replay caches. Replay reuse requires the same callback, explicit immutable rule version, input hash and costs; callers must change the version when rule configuration changes. Mutable closure changes cannot be inferred. Future rows do not invalidate past-decision cache keys.
- Every input/check error and cooperative timeout returns SKIP with a required alert callback; delivery failure remains recorded and cannot permit a trade. Three consecutive overruns reduce subsequent checks from 10,000 to 5,000 paths and emit a degradation alert. No immediate same-trade retry consumes more budget. Elapsed time and next path count are reported.
- Full suite **1315 passed, 2 skipped**; Ruff, mypy and diff checks passed. Four new tests cover short-input alerts, three-overrun degradation, future-row cache stability/input mutation invalidation, and alert failure. Cache success/degradation tests use mocked cores, not latency certification.
- Deadlines are cooperative, not hard preemption: history hashing/preparation, a slow replay rule and alert delivery can exceed 250 ms before SKIP returns. End-to-end measured-budget acceptance remains open. Caches/overrun counters are process-local and the orchestrator is not designed for concurrent threads. No engine/backtester/real-alert integration or OOS retention study is supplied; filter remains disabled and live gates remain blocked.

## Time Machine standalone Monte Carlo/verdict core (working branch)

- Added deterministic 10,000-path (or explicitly 5,000-path) block bootstrap using 6–12-bar blocks of standardized joint OHLC returns with explicit volatility scaling. Simulated stops gap-fill at open; targets rest at target; entry/stop/time slippage and two-leg fees reduce net R. Unknown dual-touch ordering uses a fair random draw and is counted, never marked verified. Returns probabilities, net R statistics, expected holding time and input/seed/path provenance.
- Added standalone fixed-threshold PASS/SIZE DOWN/SKIP logic. Mandatory nonpositive-expectancy/tail/time-exit skips override disagreement; coin-flip skip defaults on and can only reduce risk. Invalid metrics return SKIP. The function does not authorize execution and the filter remains disabled.
- Full suite **1311 passed, 2 skipped**; Ruff, mypy and diff checks passed. Added 13 core tests including seed identity, costs, symmetric random walk, verdict boundaries and malformed metrics. A symmetric fixture caught an integer-array price truncation bug during development; prices now explicitly use float64.
- Core-only single-run timings on synthetic fixtures: 10,000 paths/48 bars **56.11 ms**; 10,000 paths/288 bars **257.58 ms**. The latter exceeds the 250 ms budget before replay/preparation overhead. No timing acceptance is claimed. Cache, adaptive paths/alerts, integrated failure-skip, OOS study, strategy adapters and Phase 8 reporting remain pending; no real OOS benefit or live readiness is claimed.

## Time Machine Phase 5 foundation (working branch)

- Added analysis/timemachine.py outside risk: closed 5m point-in-time history (60-day default, 30-day minimum), strict OHLCV/continuity validation, immutable standardized OHLC returns, full-precision input hash and deterministic trade seed. Future rows are excluded before value/hash processing.
- Added historical rule replay using closed-prefix views, 60–180 days of bars, next-open market fills, long/short stops/targets, fees/slippage, gap fills, conservative stop-first ambiguous bars, censored horizons and a 20-completed-trigger sufficiency label. Reports net mean/median/p05 R and expected holding minutes. No-trigger metrics are null.
- Full suite **1298 passed, 2 skipped**; Ruff, mypy and diff checks passed. Thirteen synthetic-fixture tests cover future exclusion, immutable inputs, invalid history, prefix bounds, costs, trigger sufficiency, long/short ambiguity, gap stops, censored horizons and no analysis/AI imports from risk.
- This is not the full requested feature: Monte Carlo, verdicts, 250 ms control, cross-check cache, four-variant OOS study, strategy replay adapters, alerts and Phase 8 persistence/dashboard/report integration remain unbuilt. No filter is enabled and no performance benefit or first-touch accuracy is claimed. README documents recent-data, shared-input, news/regime and intrabar limitations; detailed pending work is in TIMEMACHINE_PLAN.md. Live gates remain blocked.

## Latest submission inspection pass (working branch)

- Added `python -m tradebot pending-submissions --mode paper` (or the configured mode) for read-only SQLite/WAL inspection of unresolved intents. A single read transaction bounds rows, individual records and combined pending/identity payload bytes; validates local identifiers/digests; and reports whether retained identity text matches. Missing/mismatching identity is diagnostic, never recovery authority. No database is created when absent.
- Full suite **1285 passed, 2 skipped**; Ruff, mypy and diff checks passed. Seven tests cover WAL-visible records, missing identity, malformed digest, all size limits, absent database and CLI output with holds preserved.
- Request snapshots can contain local strategy/order identifiers: treat command output as sensitive operational diagnostics. A matching record or empty list does not establish exchange coverage, execution absence, safe replay or permission to reset. Authenticated recovery and phases D–I remain unfinished; live gates remain blocked. Dashboard server continues port 8000 in paper/synthetic/read-only/1×; user-facing Arena access remains unresolved.

## Latest pre-callback fill bounds pass (working branch)

- Initial/polled CCXT observations now reject filled quantity above submitted quantity before cache mutation or callbacks. Cumulative quantity must not regress below either previously observed or successfully delivered quantity, preserving observations when callback delivery failed. Zero-fill averages cannot be negative. The callback emitter independently checks excessive quantity.
- Full suite **1278 passed, 2 skipped**; Ruff, mypy and diff checks passed. Fake-exchange tests cover initial/polled overfills and a regressing poll after failed partial-fill delivery. Corrected the existing fee-test fixture to request its reported quantity rather than silently testing an overfill.
- An actual venue overfill is not ignored as safe: it latches the durable observation hold and requires independent investigation; no invented corrected fill is booked. This does not establish authoritative trade identities, persisted observation watermarks, authenticated recovery or safe handling of true venue corrections. D–I remain incomplete; live gates stay blocked. Dashboard server remains on port 8000, paper/synthetic/read-only/1×, predating these changes; Arena browser access remains unresolved.

## Latest execution-total response validation (working branch)

- Shared order-manager response/recovery consistency now requires an OrderStatus enum, finite Decimal quantity/fill/average/fee, positive submitted quantity, fill quantity within [0, submitted], nonnegative average/fee, positive average for nonzero fills, and finite positive optional order price. Invalid results follow the existing durable observation hold and conflict path without a resend.
- Full suite **1275 passed, 2 skipped**; Ruff, mypy and diff checks passed. Eleven malformed fake-adapter responses cover unknown status, boolean/negative/nonfinite/overfilled quantity, missing positive average, invalid fees and infinite price. Ordinary submissions and matching timeout recovery remain passing.
- Nonnegative fee policy is deliberately conservative: negative maker rebates need separately specified authoritative accounting before acceptance. This checks returned order totals, not pre-callback CCXT fill bounds, prior external execution or venue completeness. No independently verified reconciliation, authenticated hold release or replay is supplied; D–I remain incomplete and live gates blocked. Port 8000 server continues paper/synthetic/read-only/1× and predates this pass; Arena browser access remains unresolved.

## Latest successful-response consistency pass (working branch)

- Order-manager successful placement now uses the same intent consistency check as timeout recovery before persisting an order or counting it submitted. Conflicting/absent responses latch the durable observation fault, record a conflict, attempt L1 escalation and return without a resend. An already-executed external fill cannot be undone by this check.
- Full suite **1264 passed, 2 skipped**; Ruff, mypy and diff checks passed. Nine fake-adapter cases cover inconsistent returned identity/fields and absent response, verifying one submit, no successful-submission event/count, and blocked entries.
- This is local response consistency, not verified fills, authoritative venue/account identity or authenticated recovery. Adapter callbacks may already have occurred before the response check; fill ingestion/replay and independent reconciliation remain unfinished. Live gates stay blocked and D–I are incomplete. Dashboard server remains on port 8000, paper/synthetic/read-only/1×, predating this pass; Arena gateway/browser access remains unresolved.

## Latest timeout recovery consistency pass (working branch)

- The order manager now checks lookup results against submitted intent before recording recovery: client ID, symbol, side/type, strategy/signal, strict boolean reduce-only, stop, rounded quantity and price. A conflicting result latches the observation hold, records a recovery conflict, and follows unknown-outcome handling without assuming venue absence or retrying even in paper mode.
- Full suite **1255 passed, 2 skipped**; Ruff, mypy and diff checks passed. Eight conflicting-field timeout cases verify no second submit, no recovered event and blocked entries. Existing matching recovery tests still pass.
- Local consistency is not independently verified venue completeness, account identity or authenticated recovery. Stop/time-in-force venue semantics, raw process/database mutation and in-flight orders remain outside the proof. Phase D and phases E–I remain incomplete; live gates stay blocked. The server continues on port 8000 in paper/synthetic/read-only/1× mode, but the user's reported Arena gateway timeout/QUIC failure remains unresolved; local HTTP success is not browser-access evidence.

## Latest cached client-ID intent pass (working branch)

- CCXT cached-ID return now compares symbol, side/type, stop price, reduce-only assertion, strategy/signal identity and rounded quantity/price against the original tracked order. A conflict refuses, preserves the original order and latches the durable observation hold. Equivalent rounded requests retain the ordinary process-local idempotent return.
- Full suite **1247 passed, 2 skipped**; Ruff, mypy and diff checks passed. Nine conflicting-field cases and one rounded-equivalence case use fake exchanges; no real venue certification is claimed.
- This validates the normal adapter cache only, not mutable Python objects, raw venue/store access or order-manager unknown-outcome lookup matching. Restarted adapters still refuse previously reserved identities instead of returning a reconstructed order. Sizing assertions are not quantity proofs; time-in-force/reason are not compared here. Verified reconciliation, authenticated recovery and the remaining D–I work are unfinished. Live gates stay blocked; port 8000 dashboard continues paper/synthetic/read-only/1× and predates this pass.

## Latest submission snapshot consistency pass (working branch)

- CCXT pre-network intents now retain mode, symbol, side/type, rounded decimal quantity/price, stop reference, reduce-only assertion, strategy and signal identity. Acknowledgement requires matching retained and pending records, expected fields and the venue/client digest; missing/changed records cannot silently release the pending marker.
- Full suite **1237 passed, 2 skipped**; Ruff, mypy and diff checks passed. Tests cover missing identity, independently changed records, changed venue/client identity in both records, isolation from caller mutation, and rounded request values passed to the fake venue.
- These are consistency checks, not cryptographic attestation: a privileged writer can alter matching request snapshots in both records; float conversion on network calls can lose decimal precision. Caller strategy/reduce-only assertions still do not prove computed sizing or venue-side protection. Older snapshot-less intents are not migrated and cannot be acknowledged by this path. Independent venue reconstruction, authenticated recovery and full request/account identity binding remain unbuilt. Live gates remain blocked; dashboard remains paper/synthetic/read-only/1× on port 8000 and predates these edits.

## Latest durable submission fence pass (working branch)

- Normal CCXT submission now reserves a venue/client-ID hash and pending marker with audit before network I/O. The fence is retained permanently; an adapter without a cached order refuses reuse instead of resubmitting. A pending marker blocks ordinary entries and transactional resets across restart. Validated response delivery and its audit remove only the submission-stage marker, not the identity fence or other holds. Unresolved counts appear in recovery diagnostics.
- Full suite **1231 passed, 2 skipped**; Ruff, mypy and diff checks passed. Tests cover persistent pending/reset refusal, retained identity after acknowledgement, transaction audit rollback, adapter restart duplicate refusal and network-error containment.
- This is conservative containment, not recovery. Even definite venue rejections leave a hold; there is no authenticated replay/release workflow. Earlier submissions are not migrated. Fence scope depends on adapter name/database, not independently attested account identity. Successful response acknowledgement does not prove final fills or venue completeness; poll watermarks remain local and no request/quantity binding is supplied. Raw Store writes, direct venue calls, another database and in-flight orders bypass this boundary. Identity records grow without retention (deleting them would weaken deduplication). Live promotion/resets remain blocked; D–I remain incomplete.

## Latest execution-recovery visibility pass (working branch)

- Added a single-statement diagnostic count of pending fill callbacks, pending kill incidents and invalid execution-observation marker presence. The dashboard snapshot and `/api/autonomy` expose counts without marker payloads or private order identities; the panel uses text-only rendering. Corrupt marker payloads still count. Empty counts explicitly do not establish entry permission or verified reconciliation, and no release endpoint exists.
- Full suite **1226 passed, 2 skipped**; Ruff, mypy and diff checks passed. Integration cases cover mixed corrupt markers, private-payload exclusion, snapshot/API consistency, non-clearing reads and empty-but-unverified status.
- Dashboard restarted on port 8000 with this branch code, read-only paper/synthetic/1×. The recovery API returned HTTP 200. This view is only a subset of safety holds, not a complete health assessment, authenticated recovery, venue proof or execution authorization. Phase D and phases E–I remain incomplete; live gates remain blocked.

## Latest cancellation outcome pass (working branch)

- CCXT cancellation now follows its acknowledgement with an order query and refuses success while the observed order remains open. It emits any final observed fill through the normal callback before reporting success. Cancellation errors no longer invent a canceled status; errors and unconfirmed outcomes latch the durable observation hold (local fallback if storage fails).
- Five fake-exchange cases cover a final partial fill, missing-order/network cancel failures, an acknowledgement without a terminal query result, and callback failure followed by terminal-fill retry. Full suite **1224 passed, 2 skipped**; Ruff, mypy and diff checks passed.
- Follow-up queries are not proof of venue completeness or strong consistency. This conservative implementation may block entries after an eventually consistent cancellation or transient failure; no automatic hold release is supplied. Callback watermarks and cancellation tracking remain process-local. No venue test, restart-safe cancellation replay or independent recovery is claimed. Live promotion/resets remain blocked and D–I remain incomplete. Port 8000 dashboard remains read-only paper/synthetic/1× and predates these edits.

## Latest durable observation hold and calendar pass (working branch)

- Invalid CCXT observations now first latch a local entry hold, then persist a first-fault marker and audit event in one transaction. Ordinary kill-switch entry checks and reset transactions reject the marker even if its payload is corrupt. Repeated faults preserve the first marker without unbounded duplicate audit rows. No release API is provided. Failure of the audit transaction rolls back the durable write but retains the process-local hold; complete database failure cannot protect other processes or survive restart.
- Backfill month subtraction now uses calendar month arithmetic across arbitrary year boundaries; zero, negative and calendar-underflow ranges fail before fetching. Explicit start dates continue to override months. Tests cover 12/24 months, January year crossing and leap-day dates without generating large archives.
- Full suite **1219 passed, 2 skipped**; Ruff, mypy and diff checks passed. Added restart/reset/corrupt-marker, first-fault audit and persistence-failure tests. All exchange tests remain fake responses, not venue certification.
- Added REMAINING_WORK.md, a consolidated implementation, evidence and recurring-operations register. No claim that phases D–I or the project are complete. The dashboard remains paper/synthetic/read-only/1× on port 8000; its running process predates this pass. Live promotion and live/testnet resets remain blocked.

## Latest malformed initial-order response pass (working branch)

- A successful create-order call now installs a process-local tracking record before validating response totals. Initial and polled observations use the same parse-before-mutate validation; malformed fee values are also rejected. A malformed response latches the attached local entry hold, preserves the submitted order record, and does not emit fabricated fills. Reusing its cached client ID does not submit another order.
- Eight new fake-response cases cover absent response, booleans, unparseable totals, missing average, malformed fee structures and nonfinite fees; later valid polls can deliver the fill without clearing the hold. Full suite **1209 passed, 2 skipped**; Ruff, mypy and diff checks passed.
- This is process-local containment only: no restart-safe identity/watermark journal, durable invalid-observation hold, venue trade deduplication or authenticated recovery is claimed. An absent venue ID requires client-ID lookup whose venue semantics are unverified. Network exceptions during create-order remain a separate ambiguous-outcome risk. Fee validation is not authoritative fee accounting (emitted fees still use the taker model). Phases D–I remain incomplete and live promotion/resets remain blocked. Dashboard stays paper/synthetic/read-only/1× on port 8000 and predates this code change.

## Latest malformed fill polling pass (working branch)

- Polled cumulative quantity/average parsing and validation happen before cached order mutation. Boolean, unparseable, nonfinite and inconsistent totals latch the attached kill switch's local entry hold and raise. Permanent polling errors now surface instead of being silently skipped; transient fetch errors continue to allow other orders to be polled.
- Five new fake-exchange cases verify malformed terminal responses leave cached orders open and quantities unchanged, a later valid response still delivers the fill, and that delivery does not clear the hold. Full suite **1201 passed, 2 skipped**; Ruff, mypy and diff checks passed.
- This closes the polled-response parsing gap only. Initial create-order malformed response handling, durable invalid-observation holds, independently verified reconciliation, restart-safe watermarks, authoritative venue trade identities and recovery remain incomplete. No live safety gate was relaxed. The sandbox dashboard was restored on port 8000 in isolated read-only paper/synthetic mode at speed 1 after the environment restart.

## Latest cumulative partial-fill accounting pass (working branch)

- Incremental fill price now derives from the difference between current cumulative notional (`filled × average`) and successfully delivered notional, divided by new quantity. Both process-local watermarks advance only after callback success. Numeric string quantities/averages are accepted without Python repr quoting.
- Nonfinite or regressing quantities, missing/nonpositive averages, and nonpositive incremental notional fail rather than falling back to an unrelated market/limit price. These validation failures latch a process-local entry hold when a kill switch is attached. This hold is lost on restart, is not independently verified reconciliation, and does not resolve pending fills. Malformed parsing outside this validation is not covered by this hold.
- Full suite **1196 passed, 2 skipped**; Ruff, mypy and diff checks passed. Fake-exchange tests cover changing cumulative averages, callback failure/retry, numeric strings, and inconsistent observations. Rounded venue averages, unchanged-quantity corrections, individual trade identity, actual fees, restart-safe watermarks and venue coverage remain unverified. Live promotion/resets remain blocked; phases D–I are not complete.

## Latest execution boundary and immediate-fill pass (working branch)

- CCXT placement now emits immediately reported initial fills to the callback instead of silently marking them seen; failed callbacks leave observed deltas retryable. Cached terminal client IDs no longer cause a second local create-order call. Manager, paper and CCXT reject non-boolean `reduce_only` flags before treating a request as protective.
- Full suite **1187 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Tests use fake exchange responses, not venue validation. CCXT polls cumulative average price and models taker fees rather than retrieving authoritative individual trades; partial-fill incremental prices, costs and coverage are unverified. Its idempotency cache is process-local, not a venue-wide or restart-safe guarantee. External fills before callback persistence, raw/custom venue calls, falsely labelled SELL exits and in-flight orders remain outside protections. Live promotion and live/testnet resets remain blocked; dashboard process predates changes.

## Latest signed strategy reduction pass (working branch)

- An offline-signed, database/mode/strategy/current-state-bound, single-use action can now reduce the durable multiplier of an enabled strategy. The mutation, signature consumption and audit are atomic; expiry and trust are rechecked under lock. The existing offline signer and CLI support the action. No multiplier increase, hold release or live-readiness authorization is provided.
- Full suite **1166 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Key possession is not human identity, and actual submitted quantity is not proven by the multiplier assertion. Raw SQL, custom/direct venue calls, misconfigured processes and in-flight orders remain bypasses. Live promotion and live/testnet resets remain blocked; the dashboard process predates these changes.

## Latest pending-fill inspection and local failure hold (working branch)

- `tradebot pending-fills --mode paper` (or testnet/live) reads bounded pending callback records from a read-only SQLite/WAL snapshot; malformed records fail inspection. Results are explicitly unverified and unusable for reset. Journal or key safety-persistence failures latch an additional non-clearing process-local entry hold before attempting L1.
- Full suite **1152 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. The local hold does not survive process restart or protect other processes if the database is unavailable. Inspection is not replay, exchange evidence or permission to clear a hold; no safe release exists. Direct SQL, raw/custom venue calls and in-flight requests bypass these checks. Live promotion and live/testnet resets remain blocked. Dashboard process predates these changes.

## Latest fill callback journal pass (working branch)

- Engine callbacks are journaled before queueing, with full-precision diagnostic fields and an event in one transaction. An outstanding record blocks normal entry checks and kill-switch resets, including after restart. A successful fill record and pending removal commit together; failures retain the hold. CCXT polling no longer marks a delta seen before the callback succeeds, and retries an observed terminal-order delta after callback failure.
- Full suite **1142 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. This is a hold, **not automated replay or reconciliation**: a pending callback may have been partially applied, so no automatic or authenticated clearing workflow exists. Local UUIDs are not exchange trade IDs and do not deduplicate callbacks. Venue execution before the callback/journal, process-local CCXT polling state, direct SQL/adapters, in-flight orders and total database failure remain outside this boundary. The exchange, in-memory ledger and SQLite are not atomic; live promotion and live/testnet resets remain blocked. Dashboard process predates these edits.

## Latest atomic position-close pass (working branch)

- The engine now commits the persisted position closure, shared loss streak, threshold size reduction/pause and risk audit in one transaction. Missing, stale-opened-at or already-closed positions refuse a second risk transition; failed audits roll back the close. An error attempts L1 and locally disables the strategy.
- Full suite **1135 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. The exchange fill, in-memory ledger mutation, fill recording and SQLite close are not atomic. A crash before the transaction or total database failure can still lose the close/risk transition; symbol/strategy/opened-at is not a guaranteed unique trade identity. Generic Store close, raw SQL, alternate paths and venue calls remain outside this boundary. Venue reconciliation and replay remain unverified; live promotion and live/testnet resets remain blocked. Dashboard process predates this change.

## Latest concurrent loss-streak pass (working branch)

- Shared loss-streak updates, threshold size reductions, pause extensions and their action audits now commit together. Corrupt streak state blocks normal entries; an older or equal-time win cannot reset a newer streak, and a late loss is conservatively counted. Existing stored streaks are retained. Size reductions stay latched after a win.
- Full suite **1131 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. The ledger position close and risk callback are not atomic or uniquely deduplicated: a crash can miss a callback and a duplicate can overcount; old state and trade order are not verified. Raw database/exchange access, caller assertions and in-flight orders remain outside the protection. Dashboard process predates these edits. Earlier performance results predate these sizing changes; live promotion and live/testnet resets remain blocked.

## Latest strategy risk-boundary pass (working branch)

- Risk/decay pause extensions now serialize and preserve longer or malformed holds, with pause and audit in one transaction; normal manager and CCXT entry paths recheck them. Loss-streak size reductions now feed engine risk sizing, are monotonic and audited, and are rechecked as caller-supplied assertions at placement. A win no longer automatically removes the reduction. There is no authenticated release workflow yet.
- Full suite **1118 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Consecutive-loss counting itself is still process-local and non-atomic; direct SQL, custom/raw venue access, misrepresented strategy/multiplier, reduce-only assertions and in-flight orders can bypass these controls. This does not prove actual submitted quantity or venue-side safety; dashboard process predates these changes. Earlier performance results predate this sizing change.

## Latest modeled cost-stress rerun pass (working branch)

- Paper market fills can now model increased adverse slippage relative to the decision reference; the 2× stress configuration doubles fees and modeled adverse market slippage. Limit orders remain at their limit, zero modeled slippage is not invented, and old stop references can overstate stress. Walk-forward CLI runs a separate stressed sequence over the same configured windows/parameters and independently carried capital; robustness includes its rerun result in the cost gate. Backtests, walk-forward and robustness refuse live/testnet mode.
- Full suite **1095 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. This is a simulated stress, not proof of venue costs, identical input coverage or independent OOS provenance; those gates remain unverified and live promotion remains blocked. The CLI walk-forward still does not refit. Historical results must not be retroactively treated as results of this new model. Dashboard process predates these changes.

## Latest venue/local diagnostic comparison pass (working branch)

- `venue-observe --compare-local` now reads the existing mode-specific SQLite database in a read-only transaction, including committed WAL, and reports candidate differences between venue totals and local spot positions, and between observed and locally open orders. Missing, malformed or oversized local state fails rather than being treated as clean. This is diagnostic only; even matching observations explicitly remain unverified and unusable for reset.
- Full suite **1085 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Venue calls are mocked, not validated on a real exchange; source completeness, quote cash, pending fills, ownership, fees, protective orders and atomic cross-system state remain unverified. It does not alter orders, database records or holds; authenticated reset remains blocked. Dashboard process predates the change.

## Latest read-only venue observation pass (working branch)

- Added `tradebot venue-observe --mode testnet` (also supports explicit live mode) to query CCXT total/free/used balances and the no-symbol open-orders endpoint directly, rather than relying on the in-process order cache. It checks explicit endpoint capabilities, validates response shapes/amounts, bounds results and timeouts, and performs no order mutation. Results explicitly mark completeness, atomicity and reset usability false.
- Full suite **1076 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Tests use fakes only; no real exchange was contacted. Pagination, account coverage, venue semantics, independent ledger comparison and authenticated reset remain unverified/unbuilt. Output contains sensitive account data; the Windows procedure warns about its handling. The dashboard process predates the change.

## Latest promotion-preflight integrity pass (working branch)

- The live go/no-go gate now fails closed because current promotion evidence has no verified provenance and the actual 2× rerun is not integrated. A guard check no longer overwrites/refreshes the legacy `go_nogo` record; its result goes to `live_guard_latest`. Typed confirmation requires the exact amount and a standalone LIVE token. Feed/reconciliation caller assertions must be booleans, unverified clock inputs fail, reconciliation cannot be disabled via config, and the kill preflight includes KILL and durable holds.
- Full suite **1052 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. These checks do not create verified evidence or make caller assertions independent. The live gate intentionally cannot pass until the evidence pipeline is built; live/testnet resets remain blocked. Dashboard process predates this change.

## Latest fail-closed venue-preflight pass (working branch)

- Permission checks now accept only explicit boolean or 0/1 values for withdrawal and IP restriction fields; unknown, malformed and missing responses fail closed. A failed refresh replaces an earlier cached pass, and the guard requires an actual boolean result. Live/testnet engine reconciliation starts and stays unverified instead of treating process-local open orders or free balances as independent venue evidence. Paper reconciliation is unchanged.
- Full suite **1023 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Actual venue-wide balances/open-order reconciliation, verified post-heal evidence and authenticated live/testnet reset are still unbuilt. No real exchange was contacted; the dashboard process predates this change.

## Latest live sizing-cap placement recheck (working branch)

- Normal engine entries carry the risk-sizing multiplier into the order request. The order manager and CCXT adapter recheck the multiplier against the current live cap immediately before a non-reducing placement, refusing missing, invalid or stale assertions. A test reduces the cap between manager and adapter checks and verifies no venue call; protective sells remain available.
- Full suite **962 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. The assertion is not authenticated proof that the quantity was actually sized under that multiplier. Custom callers can lie, direct SQL and raw venue calls bypass checks, and a cap change after the last check cannot stop an in-flight order. The real venue is unverified; live approval increases and reset remain blocked. The dashboard process predates the change.

## Latest signed allocation-cap reduction pass (working branch)

- Added an offline Ed25519, database/mode/action/state/expiry-bound, single-use approval that can only reduce the persisted live allocation cap. Applying it atomically records the consumed request, level-4 autonomy row and event. Normal live ramp sizing takes the minimum of this cap, configured initial cap and 10%. The real CLI and offline signing tools are tested using temporary keys and an isolated database, without connecting to an exchange.
- Full suite **951 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. This does not authorize increased allocation, implement authenticated reset, verify a human identity or certify live readiness. It does not enforce venue-side exposure limits or retroactively resize in-flight/already-sized orders; raw SQL, direct exchange/custom adapters and alternative sizing paths remain outside the boundary. The dashboard process predates the change.

## Latest durable strategy fault-budget pass (working branch)

- The first three strategy faults now atomically persist a one-hour entry pause and autonomy/event records; the fourth within the window atomically latches the disable. Multiple processes share the same serialized budget, backward clock movement does not discard future-dated faults, and engine restarts restore the dashboard view. The order-manager and CCXT entry boundaries recheck the durable pause. Audit-write failures roll back the fault transaction and attempt L1.
- Full suite **933 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Older process-local fault budgets and historical disables are not reconstructed; raw SQL/exchange access, caller-misrepresented strategy names, in-flight orders and total database failure remain outside the boundary. Fault attempt records are retained; retention policy remains open.

## Latest durable strategy-disable pass (working branch)

- A repeated-fault strategy disable is now a durable latch committed together with an autonomy record and event. Engine restart restores it; order manager and CCXT entry paths reject that strategy, while protective exits remain possible. If persisting the disable fails, the engine attempts L1 and retains its in-process block.
- Full suite **928 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Fault budgets before a disable remain process-local; old disable events are not migrated; authenticated re-enable is not implemented. Direct SQL, misrepresented strategy identifiers, raw exchange access and in-flight orders bypass this boundary. A total database failure may prevent the fallback L1 transition.

## Latest critical flapping hold pass (working branch)

- Serialize hold deadline extensions in SQLite so concurrent observers cannot shorten the hold or replace malformed safety state. Normal kill-switch entry checks now refuse active or corrupt holds even if failure occurs after hold persistence but before the L1 transition. Tests cover concurrency, malformed/null values, an interrupted transition and the actual order-manager entry boundary.
- Full suite **924 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. This does not cancel in-flight orders, protect direct exchange/SQL access or make the hold and L1 transition a single transaction; complete authenticated recovery remains open.

## Latest signed-request reviewability pass (working branch)

- Tracking-fault acknowledgement requests now include the full signed kill-state snapshot, so the operator can review the actual state rather than only its hash. The verifier requires the snapshot and hash to match the current state. Earlier requests without the snapshot must be regenerated. The integration test exercises encrypted key creation, offline signing and the real CLI with a temporary test key.
- Full suite **912 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. The acknowledgement still cannot reconstruct an incident or clear the resulting unverified-history hold, and it cannot reset a live/testnet kill switch.

## Latest kill-switch mode binding pass (working branch)

- Kill-switch construction now requires an explicit mode; normal engine and CLI callers supply it. Order-manager and CCXT entry paths reject mismatched or replaced kill switches rather than trusting an unrelated paper-mode switch.
- Full suite **911 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Direct Store writes, deliberately incorrect mode selection, private-field mutation, raw exchange access and in-flight requests remain outside this boundary. Live/testnet reset is still blocked; signed authorization and independent recovery evidence remain unbuilt. The dashboard process must restart to load this change.

## Latest live/testnet reset fail-closed pass (working branch)

- Normal engine and CLI kill switches now capture their configured mode and refuse both manual and automatic live/testnet reset until authenticated authorization and independent recovery evidence exist. Paper mode retains its previous reset paths. A caller-provided operator name is not authorization.
- Full suite **907 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Tests cover L1–L3, both live/testnet modes and the real CLI.
- This is a deliberate operational restriction, **not** a completed authenticated reset flow. Direct Store writes, incorrectly constructed kill switches, private-field mutation, arbitrary process code and external venue actions bypass the restriction. Signed tracking-fault acknowledgement still does not permit a reset, and no real exchange was tested. The dashboard process must restart to load this change.

## Latest signed tracking-fault acknowledgement pass (working branch)

- Added a short-lived Ed25519-signed request bound to database identity, mode, key fingerprint and exact kill-state snapshot. The one-use consumption and audit event commit atomically. This one action acknowledges a tracking fault, preserves the kill level and creates an unverified-history hold; it neither fabricates an incident nor permits entries/reset. The public-key trust bundle must be separately provisioned and protected; private keys remain offline.
- Full suite **897 passed, 2 skipped**; Ruff (including tools), mypy and `git diff --check` passed. Tests use generated ephemeral keys and exercise CLI, replay, stale state, expiry, wrong/revoked keys, database binding, concurrency and audit rollback.
- This is not a general approval flow or operator identity proof: it proves possession of a configured key under documented OS trust assumptions. There is no authenticated workflow to reconcile/clear the resulting hold; allocation and reset authorization, independent venue proof and complete Phase D remain open. Exact DB clones share identity. See `docs/SIGNED_RECOVERY.md`. No actual user/operator key was provisioned or real venue tested; the dashboard process must restart to load this change.

## Latest immutable risk and runtime entry-boundary pass (working branch)

- Risk-limit models and the outer config binding are frozen; hard envelope constants are class constants, cannot be configured, and are revalidated when constructing risk and order managers. Risk entry and order-manager/CCXT placement paths refuse detected runtime risk, mode or adapter changes; the order manager rejects reduce-only buys.
- Full suite **887 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Tests cover configuration spoofing, private replacement, adapter/mode changes and direct-entry bypass attempts.
- This protects these normal Python execution paths, not against arbitrary code with process/database access, direct raw-exchange calls, in-flight requests or other adapters. Protective exits remain available, but venue reduce-only behavior remains unverified. Approval authentication and consumption remain unbuilt. The dashboard process must restart to load these changes.

## Latest kill-incident outbox pass (working branch)

- Kill-switch and watchdog escalations commit a generation-bound incident outbox alongside the kill transition. Incident creation, ramp accounting, audit and removal of the matching pending record commit together. Pending records block entry and reset; kill-switch initialization retries their recording without changing the kill level. An incident-write failure no longer interrupts engine kill/flatten or watchdog continuation.
- If the outbox write fails, escalation is retried without it and a durable incident-tracking fault blocks entry and reset. Full suite **871 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Tests cover restart recovery, multiple transitions, stale completion, outbox and incident write failures, entry/reset holds, and engine/watchdog paths.
- The fallback fault has no authenticated repair/clear workflow; it intentionally remains latched. Total database failure may prevent even fallback escalation, and direct store writes, `incident=False`, external adapters and in-flight venue requests are outside this boundary. This does not prove venue flattening or complete Phase D approvals. Dashboard process must restart to load this change.

## Latest critical-incident ramp accounting pass (working branch)

- L2, L3 and CRIT records written through `Store.add_incident` now atomically commit incident, audit events and ramp counter; watchdog and kill-switch paths use that method. A corrupt ramp records the incident and a durable invalid-tracking marker instead of preventing the already-issued escalation. Live ramp sizing remains blocked even if the ramp row alone is repaired. A present JSON null ramp is now treated as corrupt, not as an absent default.
- Regression coverage includes kill switch, watchdog, concurrent writers, corrupt state and injected audit failure. Full suite **862 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed.
- Kill escalation and its incident record are **separate transactions**. A crash or incident-write failure between them can leave tracking incomplete, and the propagated error can interrupt subsequent protective actions. Other unrecorded incidents and legacy records are not backfilled. The marker is not an authenticated recovery mechanism, and direct database writes can bypass it. The dashboard process must restart to load this change.

## Latest live-ramp allocation boundary pass (working branch)

- Live ramp advancement now records a refusal instead of raising allocation; direct ramp saves cannot increase stages or clear incidents, and concurrent incident increments commit with audit events. Legacy unverified stages are capped to the initial 10% live sizing multiplier (or a stricter configured initial cap). Risk-sizing multipliers outside 0–1 or non-finite are refused; status distinguishes recorded stage from effective multiplier.
- Full suite **855 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Tests include legacy stage, direct/stale writes, concurrent incidents, audit rollback and invalid multipliers.
- This deliberately blocks promotion above the initial live ramp, rather than claiming an unimplemented approval flow. Registration is not yet wired to every real incident, raw SQL and alternate execution/sizing paths can bypass these checks, and the existing approval records still cannot authorize execution. No real venue was tested. The dashboard process must restart to load this change.

## Latest software stop-ratchet boundary pass (working branch)

- Trailing-stop tightening now commits its state and audit event together before changing engine memory. Independent SQLite writers cannot lower this ratchet; stale position saves preserve tighter persisted base and trailing stops while retaining quantity/accounting updates and auditing refused widening.
- Regressions cover restart, stale writers, engine wiring, invalid values and audit-write rollback. Full rerun: **829 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. An earlier full run failed a backfill-history assertion when the wall-clock minute advanced during the run; the targeted rerun and subsequent full suite passed. The clock-sensitive backfill test remains to be fixed.
- This is a software/database ratchet, **not** proof that a venue-side stop was updated or exists; direct SQL, arbitrary in-memory changes and live order races remain outside this boundary. Approval identity, action-bound consumption and comprehensive mutation enforcement remain open. The dashboard process must restart to load this change.

## Latest live-adapter and remote-reset boundary pass (working branch)

- The CCXT adapter refuses direct exposure-increasing submissions without an attached kill switch or when entries are blocked, including a KILL file; it also rejects a buy falsely marked reduce-only. The engine supplies its persisted switch on normal CCXT construction. Dashboard remote resume is restricted to the exact reviewed L1 state, like Telegram.
- Direct adapter, corrupt/unavailable state, kill file, protective sell and reset-race regressions pass; full suite **819 passed, 2 skipped**, Ruff and mypy passed.
- This does not cover direct raw exchange calls or arbitrary injected/other adapters. A sell labelled reduce-only is not independent proof of venue behavior; CCXT spot submission does not guarantee reduce-only and in-flight requests cannot be recalled. Approval identity, action-bound consumption and comprehensive action enforcement remain unbuilt. No real venue was tested. Dashboard process still runs its previously loaded code until restarted.

## Latest manual reset-boundary pass (working branch)

- Telegram's L1-only reset now uses the exact reviewed kill-state generation, so a concurrent L2/L3 or replacement pause cannot be cleared by the stale request. Manual reset refuses a present KILL file, missing or corrupt L3 cooldown, and corrupt flapping holds; refusals are audited. Dashboard reset refusals return a conflict rather than a server error.
- Race, corrupt-state and control-path tests pass; the complete suite passed **809 tests, with 2 skipped**, plus Ruff, mypy and `git diff --check`.
- Reset caller names are not authenticated by the kill switch itself; direct store mutations bypass it. The KILL file and SQLite update cannot be atomically coordinated, and the running dashboard needs a restart to pick up changed code. This is not independent venue reconciliation or evidence that live operation is safe.

## Latest autonomy audit-foundation pass (working branch)

- Approval requests capture independent finite JSON snapshots and durable request events. Decision transitions and their audit events commit or roll back together; failed API decisions are recorded as refusals. Nonempty durable storage, clock ordering, expiration and a self-approval check are enforced on these record paths. Ledger rows and corresponding audit events are also transactional.
- Tests inject database audit failures and race independent SQLite connections; the complete suite passed **799 tests, with 2 skipped**. Ruff, mypy and `git diff --check` passed.
- These are **unverified records only**. Claimed identities are not authenticated, no action-bound approval consumer or global mutation enforcement is wired, and old approval decisions may lack the newly added events. No approved record authorizes a trade. Audit attempts at all direct store or adapter boundaries are not yet guaranteed.

## Latest L1 recovery-boundary pass (working branch)

- Automatic L1 resume is now restricted at the kill-switch method to an allowed pause cause and the exact evaluated persisted transition. Each new transition carries a unique event generation, so recovery from an old L1 cannot clear a newer identical L1 at the same timestamp. A KILL file blocks and escalates recovery, and refused calls are audited.
- Infrastructure recovery now tracks the pause identity throughout its sustained-health window and requires aggregate feed, order errors, reconciliation flag, disk and account limits to be healthy together. Both recovery paths recheck account limits before L0, preventing a stale-feed pause from hiding a concurrent weekly loss.
- Full suite: **786 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. Tests use simulated time and paper components.
- This is not an authentication boundary: direct state writes and manual reset authorization remain open. It is not the full post-heal gate: independent venue state, complete per-symbol freshness, clock verification and the daily-loss path's infrastructure checks are not proven. Live trading remains blocked.

## Latest order-boundary safety pass (working branch)

- The order manager rechecks the persisted kill switch immediately before each non-reduce-only submission, including after a rate-limit wait and before a retry. Denials are audited; paper exits remain possible at L1–L3.
- Failed order lookups and unproven production-adapter absence no longer trigger blind retries: the outcome is logged as unknown and new entries are paused. A found order is persisted and logged as recovered. Only the in-process paper simulator can currently establish absence for a retry.
- This is not a global adapter authorization boundary, cannot undo a request already in flight, and does not establish that an exchange enforces reduce-only semantics or that an unknown order is flat. Independent venue reconciliation, authenticated approvals and live verification remain open.
- Full suite: **771 passed, 2 skipped**; Ruff, mypy and `git diff --check` passed. The tests exercise simulated adapters, not a real venue.

## 2026-11-04 — system recheck (working branch)

- Automated suite after the repository audit: **758 passed, 2 skipped**; Ruff and mypy (88 source files) passed. See `REPO_AUDIT.md` for scope and limitations.
- A 120-minute synthetic parity smoke test matched, but had **zero decisions and zero
  fills**. It is only a transport smoke test, not meaningful trade parity evidence.
- Corrected Telegram engine wiring: P&L uses the ledger's actual unrealized method;
  report requests are scheduled and their completion/failure audited; remote resume cannot
  clear L2/L3; lockdown now sets L3; flatten pauses entries before scheduling. These are
  tested with a paper engine, not against the live Telegram service.
- Live preflight now rejects large negative clock drift and future/malformed go/no-go
  records. No live exchange execution was performed.
- Paper dashboard on port 8000 responds inside the workspace and exposes read-only
  component-health and partial-autonomy audit panels. The preview gateway was unreachable
  from the user's browser; remote alert transports were not configured.
- Major self-healing/self-sustaining components remain incomplete; see Phase 6 of
  `CHECKLIST.md`. Passing automated tests is not proof of operational or trading readiness.

## 2026-11-04 — synthetic backtest and walk-forward cash correction

Run on the working tree based on `2c40d51`, with the existing default strategies and risk
settings, paper execution, synthetic seed 7 and 1,000 USDT starting equity. The workspace
had no historical-price archive; these are simulated prices, not real-market evidence.

| Check | Result | Verdict |
| --- | --- | --- |
| 10-day backtest | equity 976.93 (−2.31%), 87 closed trades, win rate 35.6%, fees 23.28 USDT, PF 0.91, Sharpe −7.60, max DD −5.67%, MC p95 DD −7.09% | NOT PROMOTABLE: PF, Sharpe and trade count fail; this is not an out-of-sample PF |
| Four-day cost stress | nominal −0.34% (38 trades); full rerun at 2× costs −0.99% (37 trades); same-period equal-weight buy-and-hold +1.35% | FAIL: both the 2×-fee estimate and the 2×-cost rerun lose money |
| Three 1-day test windows | +2.13%, −1.00%, +0.72%; compounded +1.83%; 16 trades, PF 1.85, Sharpe −2.66, max DD −1.91%, MC p95 DD −1.91% | NOT PROMOTABLE: Sharpe and trade count fail; parameters were not refit and the sample is tiny |

The walk-forward engine restarts with fresh positions for each segment and synthetic feeds
are reseeded per window. These windows are not a continuous real-market out-of-sample test.
A genuine multi-window test on recorded market data is still required.

**Accounting correction:** while running this check, we found `run_segment` and the
backtest `--capital` override changed the starting-capital label and adapter cash, but not
the ledger's cash. Carried capital could therefore silently reset to the configured default
in subsequent windows. Both paths now set ledger cash as well, with regression tests. The
first walk-forward run from this session and all earlier walk-forward figures (including
those below) are **unreliable and superseded**; the corrected figures above are from a rerun.
The standard 10-day run used the default 1,000 USDT and did not use a capital override.

Commands (PowerShell):

```powershell
python -m tradebot backtest --mode paper --feed synthetic --days 10 --seed 7
python -m tradebot robustness --mode paper --feed synthetic --days 4 --seed 7 --cost-multiplier 2
python -m tradebot walk-forward --mode paper --feed synthetic --train-days 3 --test-days 1 --windows 3 --seed 7
```

## Headline

| Run | Result | Verdict |
| --- | --- | --- |
| `backtest --days 10` | 97 trades, win 38.1%, fees 27.69, equity 971.23 (−2.9%) | **NOT PROMOTABLE**: PF 0.91 (need ≥ 1.3), Sharpe −9.77 (≥ 1.0), DD −7.63% (≤ 15%), 97 trades (< 200), MC p95 DD −8.02% (≤ 20%) |

Two of five gates pass. The three that fail are the three that matter, and the reason is
cost: 27.69 USDT of fees on a 1,000 USDT account over ten simulated days is 2.8% of equity.

## The validation suite

| Check | Command | Result |
| --- | --- | --- |
| Walk-forward, 3 windows (**invalidated by the cash carry bug; see correction above**) | `walk-forward --train-days 3 --test-days 1 --windows 3 --seed 5` | Windows +0.43%, −2.04%, +0.21% → compound **−1.42%**, "inconsistent"; OOS PF 0.20, Sharpe −11.4, 19 trades → **NOT PROMOTABLE** |
| Parameter sensitivity | `sensitivity --strategy S1_ema_trend --param ema_fast=12,20,30 --days 3` | Best cell +2.39%, neighbour median +2.35%, plateau share 1.0 → "broad plateau" — and then the multiple-testing line kills it: best Sharpe 0.02 is no better than the 1.48 expected from 3 attempts |
| Parity against a recorded tape | `parity --minutes 1200` | **MATCH**: 22/22 decisions, 8/8 fills, 24,984 tape events |
| Robustness | `robustness --days 4` | Nominal **−1.29%** (PF 0.79, 27 trades); at 2× costs **−2.25%** (PF 0.68, 26 trades). Cost stress **FAILS**; attribution: net −10.12 after 7.76 fees (77% of gross), buy-and-hold −0.36% |
| Benchmark | inside `robustness` | Buy-and-hold −0.36% over the same bars; the strategy trailed it by **1.13 points** — fees and work for less return |
| Signal screen | `signals --days 1` | 44 signals in 8 families; kept BTC 0/44, ETH 1/44, SOL 3/44, BNB 0/44, XRP 3/44. The rest are dropped as thin or unstable |
| Complexity ladder, 4 rungs | inside `signals` | Baseline retained everywhere. Net expectancy per bar: BTC base −2.5 / ridge −2.3 / boost −0.6 / seq −7.2; SOL +5.6 / +0.2 / −2.7 / −5.4 — the window model is the worst of the four and trades 2–3× as often |
| Meta-labelling | `meta --days 1 --seed 1` | SOL AUC 0.485 (gate adds −0.0 bps), XRP AUC 0.531 (+3.1 bps) → **"do not use it"**, the gain does not cover 20 bps of cost |
| Slippage | `slippage --days 2` | Per symbol p95 up to **+36.2 bps** (SOL), +26.2 (XRP), +16.1 (ETH). Calibration mean error +18.95 bps (positive = the model is *pessimistic*), so the assumption is not optimistic |
| Research loop | `research --days 2 --budget 6 --stage 0.5 --stage 1.0 --seed 1` | 6 candidates → 3 kept → 3 run: best +1.87%, deflated Sharpe 1.41, probability 0.92 against a 0.95 bar → **NO** |
| Venue costs | `venues --venue nse` | Round trip on 100,000: crypto 200.00 (0.200%), NSE 83.04 (0.083%) → "cheapest: NSE by 116.96". NSE broker reports "implemented, no keys" |
| Experiment log round trip | `experiments --export DIR`, `experiments --read DIR` | 3 runs, 6 metrics, 3 params, 12 tags, 3 non-numeric values moved to tags; all 3 read back with their params and tags |

## The cost budget, measured

`risk.min_target_cost_ratio` refuses an entry whose target is nearer than N× the round-trip
cost (two taker fees plus expected slippage from the calibrated model). On the ten-day
backtest:

| N | Trades | Fees | Equity | PF | Sharpe | Gates |
| --- | --- | --- | --- | --- | --- | --- |
| 0 (off) | 97 | 27.69 | 971.23 | 0.91 | −9.77 | NOT PROMOTABLE |
| 4 (default) | 97 | 27.69 | 971.23 | 0.91 | −9.77 | NOT PROMOTABLE |
| 8 | 97 | 27.69 | 971.23 | 0.91 | −9.77 | NOT PROMOTABLE |
| 16 | 49 | 13.74 | 1,005.06 | 1.14 | −4.15 | NOT PROMOTABLE — PF, Sharpe, 49 trades |
| 32 | 38 | 9.98 | 962.40 | 0.58 | −6.32 | NOT PROMOTABLE |

The default is inert on this data on purpose: it is a floor for higher-cost venues, not a
knob to turn until the backtest looks good. Turning it anyway shows what the data does -
PF 1.14 at N=16, then 0.58 at N=32, on a strategy that loses money - which is noise on top
of a loss, not an edge.

### A correction, recorded rather than quietly fixed

The first version of this section reported **12 trades at PF 5.54 and Sharpe 6.37** for
N=16. That was wrong: the cost estimate used `SlippageModel.fallback_bps`, which is the
*worst single fill* on record (67 bps) rather than what a fill is expected to cost. Fed
that number, ratio 16 refused almost everything and the survivors looked excellent because
there were twelve of them. `SlippageModel.expected_bps()` - the sample-weighted mean of
observed fills - now supplies the expectation, the size cap keeps using the worst value,
and the two are tested separately so they cannot be confused again. The corrected table is
above; the number it replaced is in this paragraph so a reader can see what the mistake
looked like.

## Sentiment, end to end

`news --file` and `run --news-url http://127.0.0.1:8010/bad.xml --news-interval 10`
against a local feed of four negative headlines:

- 8 polls, 32 headlines parsed, 4 kept, 28 dropped as duplicates, 0 failures
- 7 entries halved to ×0.50 at an aggregate score of −0.38 (`sentiment_sizing` events)
- the multiplier floor is 0.5 and the ceiling is 1.0: good news never enlarges an order

## MLflow live tracking, end to end

A real tracking server was started in the sandbox and a real paper session streamed into
it - no mocks on either side:

```
mlflow server --backend-store-uri sqlite:////tmp/mlflowdb/mlflow.db \
              --default-artifact-root /tmp/mlflowart --host 127.0.0.1 --port 5055
python -m tradebot run --minutes 1 --mlflow-server http://127.0.0.1:5055 --mlflow-interval 15
python -m tradebot experiments --read-server http://127.0.0.1:5055
```

- **41 points** in one minute at a 15-second interval: equity, cash, realised and
  unrealised P&L, fees, trades closed, signals, vetoes, fills, kill level
- the run is `FINISHED` at the end (the tracker terminates it), with the final session
  snapshot logged after the loop stops: equity 1,002.53, 5 trades, 1,032 signals,
  1,027 vetoes, 12 fills, win rate 0.40, kill level 0
- `experiments --read-server` reads the same run back with its params (`mode=paper`,
  `feed=synthetic`) and tags (`kind=live_session`)
- **a dead server costs three seconds, not a session.** MLflow's HTTP client retries for
  minutes; before adding the TCP pre-check, `start()` against `http://127.0.0.1:1` blocked
  for **246 s** in the test suite. With the pre-check it fails in about a second, sets
  `disabled`, reports `cannot reach 127.0.0.1:1`, and the bot trades on
- 14 new tests (12 unit against a recording fake client, 2 integration against a real
  SQLite-backed store), including the optional-dependency path: a host without MLflow
  imports, keeps running, and says why tracking is off

## Learning from its own trades

`tradebot learn` reads the closed positions the engine wrote, labels each one by net R
(P&L divided by the risk that was on the table, fees included), and runs a purged,
walk-forward gate over them. It writes a card only if the gate beats *taking every trade*
by 10% in every window, with AUC above 0.55 and the 2x-cost rerun still positive.

**The bot's own paper ledger (a live session):**

```
tradebot learn
ledger: 92 closed trades, 2026-11-04 -> 2026-11-12
  win rate 47.8%, mean +0.24R, median -0.55R, total +21.9R, fees ate 25% of gross
purged walk-forward over the ledger, 2 windows (indicative only: below the sample floor)
  window              train test    AUC     all   gated      2x    lift
  11-08->11-09           43   23  0.258  -0.069  -0.925  -1.020 -1231%
  11-09->11-12           68   23  0.158  -0.094  -0.442  -0.548  -368%
verdict: NEED_MORE_DATA
```

An AUC of 0.16-0.26 is **worse than a coin flip** - the gate ranks winners below losers -
and its "gated" expectancy is far worse than simply taking every trade. On 43-68 training
rows a 13-feature logistic model fits noise and generalises backwards. This is the single
best argument for the 200-trade floor: had the floor been 50, this card would have been
written, installed and would have made a losing strategy lose faster.

**A persisted 20-day backtest (101 trades, stopped at the timeout):**

```
tradebot backtest --days 20 --persist /tmp/backtest.db
tradebot learn --db /tmp/backtest.db
  window              train test    AUC     all   gated      2x    lift
  01-07->01-09           49   25  0.480  -0.037  -0.027  -0.137   +28%
  01-09->01-11           74   26  0.429  -0.331  -0.363  -0.487   -10%
verdict: NEED_MORE_DATA   (101 trades, 200 required)
```

Note what the "+28%" window is worth: lift on a negative base, with AUC 0.48. Nothing.

**The machinery's positive path, on a constructed ledger** (300 trades, outcome planted
so that entry-time ATR predicts the result - a fixture, not a result):

| ledger | windows | AUC | gated R | 2x R | verdict |
| --- | --- | --- | --- | --- | --- |
| planted edge | 3 | 1.000 | +1.000 | +0.950 | **PROMOTABLE** -> card written |
| pure noise | 3 | 0.500 | -0.013 | -0.063 | **NO_EDGE**, no card |

A card written from the first was installed with `run --ledger-gate` and refused the
low-ATR setups in the engine's own decision path; the second writes nothing and says why.

**Every attempt is a trial.** `learn` writes a `ledger_gate` row into the `experiments`
table whatever the verdict says, so running it twenty times and keeping the best card would
show up in the log the multiple-testing report reads:

```
  2026-11-03T19:17:12  ledger_gate  ledger   ledger-20261103T191712  trades=92, win_rate=0.413, mean_r=0.0524
```

**A card expires.** `run --ledger-gate` loads it with `live_guards.go_nogo_max_age_days`
(default 14) and a card past that age is inert: it vetoes nothing, prints why on stderr, and
says the same in every verdict reason. A card is a statement about a regime, and a stale one
keeps refusing trades with confident-looking reasons long after the market that produced it
is gone - the failure mode is identical to a real one, which is why the age check is not a
formality.

**What this does not do:** it does not retrain a strategy, does not change position size
(the risk engine owns that), and cannot add a trade. The only thing a ledger card may do is
refuse an entry, which is the same veto-only contract as the meta-label gate.

## Replaying the archive, end to end

The archive was write-only for a while: `backfill` recorded Parquet, and no research
command could read it. That is now closed, and the failures found while closing it are
worth recording because each one produced a plausible-looking wrong answer.

| What was wrong | What it looked like | Fix |
| --- | --- | --- |
| Sim clock hard-coded to 2024-01-01 | a 2026 archive replayed 0 trades, gates FAIL `trade_count` | start on the archive's first bar, clip the window to its last |
| Replay emitted bars only | 54 signals, 24 orders, **0 fills**, 0 trades | model a top-of-book from each bar close; print that caveat |
| Demo fetcher priced from the absolute epoch minute | demo prices at `exp(598) ≈ 1e259` | a 30-day continuous cycle, so 2024 and 2031 look alike |
| A NaN stop reached `save_signal` | `decimal.InvalidOperation` killed the run | refuse the signal, log `signal_rejected`, keep the session |

Evidence from three days of the offline demo tape (43,200 1m bars per symbol, five
symbols, real pagination, invented prices):

```
feeds  book modelled ...  21591 bars, 21591 books, 54 signals, 24 orders, 24 fills, 8 closed
closed trades: 8 | win rate 1.000 | fees 3.00 | end equity 1020.58
FAIL  out_of_sample_profit_factor  PF 0.00 >= 1.3
PASS  sharpe                       sharpe 16.03 >= 1.0
PASS  max_drawdown                 max DD -1.11% <= 15%
FAIL  trade_count                  8 trades >= 200
verdict: NOT PROMOTABLE
```

A 100% win rate and a Sharpe of 16 on a demo tape are a property of the tape, not of the
strategies, which is exactly why the gates are not read from a synthetic run. The number
that matters is that the replay now moves real orders through the real engine: 24 orders,
24 fills, 8 closed trades with fees. `walk-forward`, `sensitivity`, `robustness` and
`signals` were also run on `--feed archive` and produced results rather than tracebacks
(`signals`: nothing survived the screen, baseline promoted - the honest answer for a
4-bar horizon against 10 bps a side).

## What is not measured here, and why

Live order placement, a WebSocket session, a real backfill, Telegram's API and FinBERT
weights all need network egress or credentials this sandbox does not have. A replay's
depth is modelled, not measured: every fill in the archive results above happened against
a book built from the bar close. MLflow live
tracking is *not* on this list any more: a tracking server runs locally, so it was verified
rather than deferred. Each is listed
with its next step in `ROADMAP.md` → "Not built". The live ccxt adapter's logic is covered
by 27 tests through a fake exchange, but no test here can prove what Binance will answer.

# Remaining work — implementation and acceptance register

Updated 2026-11-02. Applies to working branch `arena/200089ad-delhi-aqi`, draft PR #8, not a claim that these changes have merged. This consolidates the open items in CHECKLIST.md, BUILD_PLAN_20_21.md, SIGNED_RECOVERY.md and WINDOWS_LOCAL_LIVE_READINESS.md. Those documents retain detailed procedures and caveats. No item below is complete merely because it is listed or has a diagnostic command.

**Status:** [x] implemented with local automated tests; [ ] incomplete. **External** means sandbox tests cannot establish acceptance. Live trading and live/testnet resets remain blocked. Do not manually delete holds to make checks pass.

## New pre-trade Time Machine requirement

- [x] Phase 5 bar/return and generic replay foundations, outside risk, tested on synthetic fixtures.
- [x] Bounded decision-time Parquet history provider wired through the research engine bridge; no resampling, synthetic fallback or gap filling. Real archive provenance/coverage and latency remain unverified.
- [x] Deterministic existing-strategy context/replay bindings with versioned settings and market EMA plan parity.
- [ ] Complete strategy-specific limit-order entry/fill semantics, default-strategy replay parity and stateful ML support; existing bindings explicitly refuse unsupported entries.
- [x] Phase 6 standalone Monte Carlo/verdicts, simulated risk-reducing engine bridge, bounded caches/cooperative deadlines, alert callbacks, concrete four-engine research runner and evidence CLI.
- [ ] Phase 6 hard end-to-end250ms acceptance, external alert verification, supported entry/exit parity and meaningful recorded four-variant OOS retention study.
- [x] Phase 8 research-engine audit persistence, dashboard check panel beside Trades, computed order tickets and bounded daily skipped/down lists. External alert delivery and live integration remain unverified.
- [ ] Verify on real recorded OOS data without final-holdout tuning before enabling; otherwise keep disabled.

Full criteria and honest limits: [TIMEMACHINE_PLAN.md](TIMEMACHINE_PLAN.md).

## D — execution, authority and durable recovery (finish before E–I integration)

- [x] Confirm CCXT cancellation through a follow-up order query and deliver final observed fills; failed or unconfirmed outcomes latch the durable observation fault. Venue consistency and restart-safe cancellation recovery remain unverified.
- [x] Persist invalid CCXT observation hold with first-fault audit; check it on ordinary entries and transactional resets across restart. Failed persistence retains the local hold. No release workflow is supplied.
- [x] Persist CCXT pre-submission client identity fences and unresolved-submission holds; duplicate client identities are refused after adapter restart.
- [ ] Reconcile ambiguous submissions with verified venue identities and authenticated recovery. The fence has no migration of earlier orders, request/quantity proof, automatic replay or release workflow.
- [ ] Retrieve authoritative venue trade identities, fees/currencies and pagination; detect missing/duplicate/late/corrected trades and cumulative-average rounding.
- [ ] Introduce stable position IDs; remove symbol/strategy/open-time identity ambiguity and migrate existing databases safely.
- [ ] Make fill application replayable and idempotent with durable transaction boundaries covering position/risk/fill state; rebuild in-memory ledger safely after crashes.
- [ ] Independently reconcile complete venue balances, cash, open orders, fills and protective orders against the local ledger with explicit coverage/freshness provenance. Diagnostic comparisons are not verification.
- [ ] Build authenticated, evidence-bound recovery/replay/release for pending fills, observation faults, incident-history holds, strategy disables and size reductions. Invalid/missing evidence must refuse release.
- [ ] Complete action-bound one-use approvals for allocation increases, ramp advances, re-enable and live/testnet reset, with independent health/reconciliation evidence, expiry and concurrent-consumption tests.
- [ ] Provision and protect the operator trust bundle outside the trading process. **External:** verify OS permissions and offline signing/key rotation/revocation procedures.
- [ ] Enforce actual quantity and protective SELL constraints rather than trusting caller labels or multiplier assertions; prove venue semantics. **External:** validate exchange-side behavior.
- [ ] Cover all supported mutation paths/adapters and healer actions with forbidden-action tests; document remaining raw SQL/Python/venue privilege boundary and constrain deployment credentials.
- [ ] Migrate or explicitly quarantine legacy audit/incident/fault/streak history; do not fabricate missing history.
- [x] Read-only process/durable entry-blocker reasons surfaced in dashboard and research-study integrity; no release authority.
- [x] Carry read-only entry-blocker reasons through engine health, health CLI and component Health API separately from component scores.
- [ ] Complete durable-fault/promotion evidence coverage in preflight and alerts; verify consistency across all consumers.

## Remaining self-healing requirements (§20)

- [ ] Wire five feed failures in ten minutes to bounded REST fallback and L1; test restoration and rate limits.
- [ ] Implement candle mismatch detection, authoritative REST repair and marked repaired windows without fabricated candles.
- [ ] Implement safe memory-pressure restart with durable restart budget and independent recovery verification.
- [ ] Implement bounded database-lock handling, integrity checks, verified backup restoration and replay without overwriting active/unreviewed state.
- [ ] Implement safe clock-drift response and platform-specific NTP remediation/verification; never assume privileged resync succeeded.
- [ ] Implement model-registry rollback, rules fallback and flat escalation; quarantine feature NaN spikes.
- [ ] Implement validated config reload with last-known-good retention and immutable risk envelope.
- [ ] Implement outage/status polling and complete per-symbol feed, clock, venue and account post-heal gates before any eligible resume.
- [ ] Complete independent watchdog venue client, protection quantity/side checks and dead-man behavior; keep live/testnet automatic restart blocked until verified.
- [ ] Complete managed/unmanaged process ownership checks, heartbeat generation and safe stop/restart behavior on Windows and deployment hosts.
- [ ] Implement deployment health-window rollback and test it on the actual runtime. **External:** Docker/host execution.
- [ ] Add uptime, flapping, heal and escalation reporting with persistent evidence; exercise every new playbook row with failure-injection tests.

## E — market graph (§21.2)

- [ ] Persist asset/strategy/source graph and provenance; compute correlation, lead-lag, beta and contagion signals without look-ahead.
- [ ] Integrate bounded, risk-reducing contagion exposure cuts; test stale/missing/corrupt input, concurrency and audit.
- [ ] Validate graph behavior on recorded market data, not only synthetic fixtures. **External data required.**

## F — allocator (§21.3)

- [ ] Implement inverse-volatility weights and shrunken performance tilt with stable numerical behavior and stale/missing-data handling.
- [ ] Enforce 35% per-strategy bound, at least 20% cash, no more than 10 percentage points change per week, and no rebalance at L1 or above.
- [ ] Integrate authenticated approval for increases, durable weekly rebalance identity and audited risk-reducing cuts.
- [ ] Compare against equal weighting on identical out-of-sample inputs after costs; retain equal weights unless the alternative earns deployment. **External data required.**

## G — strategy lifecycle (§21.4)

- [ ] Implement all nine specified lifecycle states, durable gated transitions and authenticated promotion/re-enable.
- [ ] Integrate decay detection, automatic disable, review candidates and 60-day retirement; cover late data and concurrent workers.
- [ ] Validate lifecycle/decay against recorded histories and adversarial cases without inventing performance.

## H — funding, learning and operator absence (§21.5–21.8)

- [ ] Build cost ledger, cost attribution, self-funding ratio, break-even capital, reserves/profit waterfall and budget caps. No negative/unknown balance may fund new risk.
- [ ] Schedule drift monitoring, weekly retraining, monthly research and quarterly audit with persistent idempotent job records; no automatic promotion.
- [ ] Implement authenticated operator check-ins and absence ladder: seven-day warning, fourteen-day half size, twenty-one-day L1; survive restart and clock anomalies.
- [ ] Produce weekly digest of costs, performance, approvals, incidents, drift, lifecycle and absence status, with delivery evidence.

## I — dashboard, drills and release

- [ ] Complete Health/Autonomy views, approval authorization, full audit coverage, durable fault visibility, go/no-go and MTTR metrics.
- [ ] Secure dashboard read access at the deployment boundary; controls remain disabled by default. **External:** HTTPS/authenticated proxy verification.
- [ ] Exercise every playbook row in paper game day; record feed <10 s, worker <5 s and restart/reconcile <60 s targets as measured results, not guarantees.
- [ ] Verify actual Arena browser access, not just localhost HTTP response. Dashboard must remain on port 8000, paper/synthetic/read-only at speed 1.
- [ ] Verify Windows 11 + PowerShell procedures, backup/restore and process management on the user's host. **External.**
- [ ] Run Docker/compose health and rollback tests, package install/upgrade and clean database migration tests. **External runtime where unavailable here.**
- [ ] Update old overstatements in runbooks and release docs; reconcile checklist statuses with implemented behavior and evidence.
- [ ] Run CI, inspect the final combined draft PR #8, mark ready and merge only after the agreed acceptance scope genuinely passes. Do not create separate phase PRs.

## Data, strategy and live acceptance — cannot be simulated into completion

- [x] Fix backfill calendar subtraction for 12+ months and test year/leap-day boundaries plus invalid ranges.
- [ ] Acquire and verify sufficient real historical data, symbol/time coverage, timestamps, missing chunks and cost sources; preserve provenance and resumability.
- [ ] Integrate verified input/cost/OOS provenance with walk-forward, robustness and all promotion consumers; compare identical nominal and actual 2× modeled-cost reruns.
- [ ] Evaluate at least three real OOS windows and the §19.2 approximately 10% net-edge criterion without parameter leakage.
- [ ] Require OOS PF >1.3, Sharpe >1.0, drawdown <15%, at least 200 OOS trades, a profitable actual 2× cost rerun and Monte Carlo p95 drawdown <20%. A failed strategy is a valid result, not permission to relax gates.
- [ ] Demonstrate meaningful recorded/replay decision and fill parity; zero-decision synthetic matches are insufficient.
- [ ] Run real-price paper trial, calibrate actual execution costs/slippage, and measure flatten/fire-drill outcomes. **External exchange connectivity/data.**
- [ ] Verify real Telegram/webhook delivery, failure retry and independent alert channels; optional integrations remain optional. **External configuration/connectivity.**
- [ ] Validate testnet permissions, orders, partial fills, cancellation, reduce-only/protection and reconciliation, then complete the two-week testnet trial. **External credentials, network and elapsed time.**
- [ ] Only after implementation, approval, provenance and trial gates pass: supervised live-readiness assessment and live ramp with twice-daily operator review. This is not currently authorized.

## Recurring operational duties (not one-off implementation completion)

- [ ] Export experiment/multiple-testing audit trails; prune archives according to retention while preserving required evidence.
- [ ] Back up databases and archive; perform verified restoration drills.
- [ ] Conduct monthly game day, quarterly audit and continuous cost/performance/health review.
- [ ] Record durable numbers and failures in RESULTS.md; never report synthetic evidence as venue validation.

The operator's quote currency, symbols, capital cap, hosting and optional integration choices remain deployment configuration, not facts the agent may silently decide to enable real-money trading.

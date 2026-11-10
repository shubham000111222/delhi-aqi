# Results — measured, reproducible, and mostly negative

The historical results farther below were run on **2026-11-03** from commit `8486021` (plus the dashboard veto
panel) in this repository, with the default config, on the **seeded synthetic market**.
Nothing here is a claim about real crypto markets: the synthetic feed has regime shifts and
correlated moves, which is enough to exercise the plumbing and nowhere near enough to
establish an edge.

Reproduce any row with the command in it. If a number here disagrees with a rerun, the
rerun is right and this file is stale — fix it rather than the other way round.

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

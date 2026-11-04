# Build plan §20–21: self-healing and self-sustaining

Companion to the main build plan's sections 20 (self-healing, R29) and 21
(self-sustaining, R30). This file is the **status sheet**: what each requirement needs,
what exists today, and the order the rest lands in. It is updated with every PR, so
"tracked" here always means "in `main`", never "planned".

Legend: ✅ built and tested · 🟡 partial · ⬜ not started · 🔒 needs a human or a networked
machine (cannot be verified in this sandbox)

---

## 20. Self-healing (R29)

> *Heal infrastructure automatically, heal trading decisions conservatively. Healing never
> adds risk. The exchange is the source of truth. Verify before resuming. Bounded and logged.*

### 20.2 Health model — 🟡 built in PR A

Every component answers three questions - **liveness** (running), **readiness** (can do its
job), **correctness** (output is sane) - which collapse to `HEALTHY` / `DEGRADED` /
`FAILED`. `src/tradebot/health.py` holds the model and no policy.

| Component | Liveness | Readiness | Correctness | State |
| --- | --- | --- | --- | --- |
| `feed` | feed task alive | not all symbols stale | no per-symbol staleness | ✅ |
| `candles` | bars flowing | first price seen | feed not wholesale stale | ✅ |
| `strategies` | none disabled | none paused | no faults in 10 min | ✅ |
| `orders` | adapter connected | no entry past its deadline | fills ≤ submitted | ✅ |
| `store` | writable (heartbeat) | disk below the pause threshold | reconciliation `ok` | ✅ |
| `adapter` | adapter connected | error rate below `exchange_error_rate_limit` | reconciliation `ok` | ✅ |
| `alerting` (optional) | — | notifier configured | — | ✅ |
| `ai` (optional) | — | — | no strategy disabled by the supervisor | 🟡 placeholder until a model card is live |

Where it is visible: `tradebot health` (CLI), `GET /api/health` and the snapshot's
`health.components` (dashboard), and **"Component health"** inside section 6 of the daily
report. A state change writes a `health_change` event - the audit trail §20.4 asks for.

### 20.3 Heal playbook — 🟡 (B: six rows wired, verified, audited)

| Playbook row | Today |
| --- | --- |
| WebSocket drop / stale feed → reconnect with backoff, REST gap fill | ✅ `WebSocketFeed` backoff + gap fill, and B: `feed_stale` row reconnects and verifies against a fresh tick |
| 5 failures in 10 min → fall back to REST polling → L1 | 🟡 backoff and L1 exist, the explicit 5-in-10 fallback is not wired |
| API errors / 429 → back off, lower rate, open a 60 s circuit breaker | ✅ B: `CircuitBreaker` (open 60 s, one half-open probe) + `api_pressure` row halves the rate bucket and verifies with a real `get_open_orders()` |
| Stuck order → query by client id, cancel, reconcile | ✅ in two halves. **Entries** were already covered by `expire_stale_entries` (query by client id → cancel → reconcile). **Exits** were the real gap: `_exiting` was only a 120 s re-entrancy guard, so a market exit the venue ignored left the position open with no query, no cancel and no retry. B's `exit_stuck` row clears the guard and re-submits at market, and fails after three re-submissions so the ladder takes it to L2 instead of looping |
| Strategy exception → restart, re-warm, 3 restarts/hour, then disable | ✅ PR A: `strategy_faults` (3/hour) then `strategy_disabled` + CRIT event |
| Candle mismatch → repair from REST, mark the window | ⬜ |
| Process crash → restart, reconcile before trading | ✅ (startup recovery + reconciliation gate) |
| Position open with no protective order → place the stop | ✅ B: `position_unprotected` row calls `_ensure_protection` and verifies `stop_order_id`; paper mode is skipped because the local monitor is the stop there |
| Hung process → watchdog restarts; flatten first if a position is unprotected | 🟡 watchdog escalates to L2 and can flatten; no restart |
| Database lock / corruption → retry, checkpoint, restore + replay | 🟡 WAL + retry; restore/replay is a runbook step, not code |
| Disk filling → purge per retention, then L1 | ✅ `apply_retention` + thresholds, and B: `disk_pressure` row purges, then verifies the fill actually dropped |
| Ledger/adapter disagree → reconcile, and if it still disagrees, stop | ✅ B: `reconcile_mismatch` row cancels an order the venue holds and we do not, re-reconciles, and verifies the result is `ok`. Found by running a real five-minute session, where the first version "reconciled" and reported the identical mismatch |
| Memory growth → controlled restart at a safe moment | ⬜ |
| Clock drift → NTP resync, then L1 | 🟡 drift measured in `live-check`; no forced resync |
| Model failure/drift → roll back, then rules, then flat | 🟡 `MlSignal` returns nothing without an approved card (fails closed); no registry rollback |
| Feature NaN spike → quarantine the signal | 🟡 NaN stops are rejected at the signal boundary; per-feature quarantine is not built |
| Strategy decay → auto-disable + candidate for review | ⬜ (needs §21.4 lifecycle) |
| Alert/report delivery failure → retry queue, alternate channel, disk always | 🟡 reports always written to disk; no retry queue or second channel |
| Invalid config on reload → keep last known good | ⬜ |
| Failed deployment → roll back if health checks fail in 5 min | 🔒 Docker/compose concern, needs a deployment to test |
| Exchange outage → pause, poll status, resume after health + reconciliation | 🟡 stale-feed L1 + auto-resume after sustained health; no status-page poll |

### 20.4 Escalation and guardrails — 🟡

| Rule | Today |
| --- | --- |
| Ladder: retry → restart → fallback → degraded → L1 → L2 → L3 | ✅ L0–L3 with triggers, incidents and auto-resume for infrastructure causes |
| Restart limits: 3 per component per hour | ✅ `RestartBudget`, enforced for strategies (PR A) |
| Every heal action is verified, unverified counts as failure | ✅ B: `Playbook` runs detect → act → verify; a false verify writes `heal_failed` and counts toward escalation |
| Escalation is once per burst, never per attempt | ✅ B: 3 failures inside 10 min escalate once, then the window clears |
| Bounded retries: cooldown per row | ✅ B: 30 s-30 min per row, so a broken repair cannot become a busy loop |
| Healing may only cancel, reduce, close, protect | ✅ B: `SAFE_ACTIONS` is an allowlist and every wired row uses one of them |
| Flapping detector: >5 state changes in 10 min holds at L1 | ✅ detected and reported by `HealthModel.flapping`; **not yet wired to force L1** |
| Post-heal checklist before returning to L0 | 🟡 auto-resume requires reconciliation `ok` + 10 min sustained health; the full checklist is not a gate |
| Auto-resume only from L1, infrastructure causes only | ✅ (`AUTO_RESUME_TRIGGERS`) |
| Audit: every heal action logged | ✅ `health_change`, `strategy_paused`, `strategy_disabled`, `kill_switch`, incidents |

### 20.5 Supervision layers — 🟡

| Layer | Today |
| --- | --- |
| In-process supervisor tree | 🟡 components report health; per-component restart policy is not built |
| External watchdog process | ✅ heartbeat, kill-file, L2 escalation, optional flatten |
| Container restart policy + health checks | ✅ `docker-compose.yml` + `/healthz` |
| Dead man's switch (live) | ⬜ |
| Independent alert channel | ⬜ (Telegram only) |

### 20.6 Testing and measuring — 🟡

| Requirement | Today |
| --- | --- |
| One test per playbook row | 🟡 B covers all six wired rows (`tests/integration/test_heal_engine.py`) and the machinery (`tests/unit/test_heal.py`); the unwired rows in the table above cannot be tested because they do not exist yet |
| Injection tests fail first, then get repaired | ✅ B: the stale-feed test asserts the repair *cannot* verify in replay and that escalation follows; the reconcile and stuck-exit tests assert the repair verifies |
| MTTR targets (feed <10 s, worker <5 s, restart+reconcile <60 s) | ✅ per-row `mttr_seconds` recorded on every outcome, compared with the row's target and printed by `tradebot health` |
| Monthly game day in paper mode | ⬜ |
| Reported metrics: uptime, MTTR, heals, escalations, flapping | 🟡 `Playbook.metrics()` gives attempts/verified/p50/p95/target per row; uptime and flapping reporting are still event-only |

### 20.7 What self-healing cannot fix — ✅ documented

A dead edge, a deterministic bug, exchange insolvency, or a wrong design assumption are all
stated in the plan and reflected here: the system disables and escalates, it does not
invent a fix.

---

## 21. Self-sustaining (R30)

Four loops and an overseer. **Operate** is built; **heal** is above; **learn** exists in
shadow form; **fund** does not exist.

| § | Requirement | Today |
| --- | --- | --- |
| 21.1 | Four loops + overseer | 🟡 operate ✅, heal 🟡, learn 🟡 (research + shadow + gates exist; no scheduler), fund ⬜ |
| 21.2 | Market graph (assets × strategies × sources; correlation, lead-lag, beta, contagion cut) | ⬜ correlation is computed ad hoc in research only |
| 21.3 | Allocator: inverse-vol → shrunken performance tilt → bounds (35% per strategy, 10 pp/week, 20% cash), weekly rebalance never at L1 | ⬜ equal weights only |
| 21.4 | Lifecycle manager: IDEA→…→RETIRED, gated transitions, decay auto-disable, 60-day retirement | ⬜ strategies are static in config |
| 21.5 | Fund loop: cost ledger, self-funding ratio, break-even capital, profit waterfall, budget caps | 🟡 tax reserve + daily report ✅; the rest ⬜ |
| 21.6 | Learn schedule: continuous drift, weekly retrain, monthly research, quarterly audit | 🟡 research loop and trial log exist; no scheduler |
| 21.7 | Autonomy limits, approvals, autonomy ledger, forbidden-action tests | 🟡 confirm-with-TTL for pause/kill ✅; autonomy ledger and forbidden-action suite ⬜ |
| 21.8 | Operator-absent ladder (7 d warn, 14 d half size, 21 d L1) | ⬜ |
| 21.9 | Build order: operate + heal first | ✅ this file follows it |
| 21.10 | Honest limits + scenarios 29–34 | 🟡 stated in docs; the mechanisms are the rows above |
| 21.11 | Dashboard Autonomy page + go/no-go additions | 🟡 `/api/health` exists; Autonomy page ⬜ |

---

## Build order

| PR | Scope | State |
| --- | --- | --- |
| **A** | Health model, component checks, `tradebot health`, `/api/health`, report section, restart budget for strategies, audit events | ✅ merged |
| **B** | Heal playbook table + circuit breaker (60 s, half-open probe) + stuck-**exit** sweep + verification, cooldowns, escalation + MTTR + one injection test per wired row | ✅ this PR |
| **C** | Watchdog v2: restart on lost heartbeat, flatten-if-unprotected first, dead man's switch + second alert channel | ⬜ |
| **D** | Autonomy ledger + approval flow + forbidden-action tests (§21.7) + flapping forces L1 | ⬜ |
| **E** | Market graph (§21.2) and the contagion exposure cut | ⬜ |
| **F** | Allocator (§21.3) with the equal-weight backtest comparison as its gate | ⬜ |
| **G** | Lifecycle manager (§21.4) with decay auto-disable | ⬜ |
| **H** | Fund loop (§21.5), operator-absent ladder (§21.8), weekly digest | ⬜ |
| **I** | Dashboard Health + Autonomy pages, MTTR metrics, monthly game day | ⬜ |

### Non-negotiables carried into every PR

- Healing may only cancel orders, reduce or close positions, and place protective stops.
- Risk and kill-switch limits stay read-only to the trading process; a model may never
  change them.
- Every autonomous action is logged; approval requests carry numbers and expire.

### What PR B deliberately did not do

- **Dead man's switch and the second alert channel** (PR C). Both need a process that
  outlives the bot, so they belong with watchdog v2.
- **Nothing that touches risk limits.** The playbook's only escalation path is
  `killswitch.trigger`, which is the same lever a human pulls; it cannot raise limits,
  resize positions upward, or re-enable a disabled strategy.
- **Unwired rows stay unwired.** Candle repair, memory restart, clock resync, config
  keep-last-good, alert retry queue and exchange status polling are listed as ⬜ above
  rather than being claimed by a table entry.
- If a mechanism cannot be verified here, it is marked 🔒 rather than claimed.

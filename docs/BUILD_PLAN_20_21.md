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

### 20.3 Heal playbook — 🟡

| Playbook row | Today |
| --- | --- |
| WebSocket drop / stale feed → reconnect with backoff, REST gap fill | ✅ (`WebSocketFeed`: backoff, resubscribe, gap fill) |
| 5 failures in 10 min → fall back to REST polling → L1 | 🟡 backoff and L1 exist, the explicit 5-in-10 fallback is not wired |
| API errors / 429 → back off, lower rate, open a 60 s circuit breaker | 🟡 backoff + rate limiter + error-rate L1; **no circuit breaker object** |
| Stuck order → query by client id, cancel, reconcile | 🟡 `expire_stale_entries` cancels on deadline; no query-by-id pass |
| Strategy exception → restart, re-warm, 3 restarts/hour, then disable | ✅ PR A: `strategy_faults` (3/hour) then `strategy_disabled` + CRIT event |
| Candle mismatch → repair from REST, mark the window | ⬜ |
| Process crash → restart, reconcile before trading | ✅ (startup recovery + reconciliation gate) |
| Hung process → watchdog restarts; flatten first if a position is unprotected | 🟡 watchdog escalates to L2 and can flatten; no restart |
| Database lock / corruption → retry, checkpoint, restore + replay | 🟡 WAL + retry; restore/replay is a runbook step, not code |
| Disk filling → purge per retention, then L1 | ✅ (`apply_retention`, disk warn/pause thresholds) |
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
| One test per playbook row | 🟡 5 health wiring tests + 16 model tests; the rest of the table is not covered |
| MTTR targets (feed <10 s, worker <5 s, restart+reconcile <60 s) | ⬜ nothing measures MTTR yet |
| Monthly game day in paper mode | ⬜ |
| Reported metrics: uptime, MTTR, heals, escalations, flapping | 🟡 events are recorded; no aggregate report |

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
| **B** | Heal playbook table + circuit breaker (60 s, 429-aware) + stuck-order query-by-id + one injection test per row | ▶ next |
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
- If a mechanism cannot be verified here, it is marked 🔒 rather than claimed.

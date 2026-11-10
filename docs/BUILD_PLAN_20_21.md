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
| Alert/report delivery failure → retry queue, alternate channel, disk always | ✅ C: `alerts/router.py`. The file floor is written *before* any network call; Telegram and an independent webhook are both attempted; an alert no remote channel took is queued to JSONL and retried by `flush()` (a minute apart, ceiling on attempts and age). `tradebot alerts --test` proves the path end to end |
| Invalid config on reload → keep last known good | ⬜ |
| Failed deployment → roll back if health checks fail in 5 min | 🔒 Docker/compose concern, needs a deployment to test |
| Exchange outage → pause, poll status, resume after health + reconciliation | 🟡 stale-feed L1 + auto-resume after sustained health; no status-page poll |

### 20.4 Escalation and guardrails — 🟡

| Rule | Today |
| --- | --- |
| Ladder: retry → restart → fallback → degraded → L1 → L2 → L3 | ✅ L0–L3 with triggers, incidents and auto-resume for infrastructure causes |
| Restart limits: 3 per component per hour | ✅ `RestartBudget` for strategies (PR A) and for process restarts (C); the supervisor refuses the fourth and escalates rather than retrying |
| Every heal action is verified, unverified counts as failure | ✅ B: `Playbook` runs detect → act → verify; a false verify writes `heal_failed` and counts toward escalation |
| Escalation is once per burst, never per attempt | ✅ B: 3 failures inside 10 min escalate once, then the window clears |
| Bounded retries: cooldown per row | ✅ B: 30 s-30 min per row, so a broken repair cannot become a busy loop |
| Healing may only cancel, reduce, close, protect | 🟡 `SAFE_ACTIONS` is not an enforcement boundary. The order manager rechecks the kill switch before each non-reduce-only submit and refuses ambiguous-outcome retries; this does not cover direct adapter calls, in-flight requests or independently verify venue reduce-only behavior. Actual mutation paths need explicit enforcement and adversarial tests. |
| Flapping detector: >5 state changes in 10 min holds at L1 | ✅ working branch: critical flapping forces L1, persists the hold and recent transitions, and blocks L0 reset until settled |
| Post-heal checklist before returning to L0 | 🟡 infrastructure auto-resume checks the same pause identity, feed aggregate, error rate, reconciliation flag, disk and account limits for the sustained interval; the complete independently verified venue, per-symbol feed and clock checklist is not a gate. The daily-loss path checks account limits but does not run the full infrastructure checklist. |
| Auto-resume only from L1, infrastructure causes only | 🟡 kill-switch boundary restricts L1 to named infrastructure causes and a separate daily-loss exception; callers are not authenticated and must supply the exact evaluated state. |
| Audit: every heal action logged | ✅ `health_change`, `strategy_paused`, `strategy_disabled`, `kill_switch`, incidents, `heal*`, `watchdog_supervisor`, `alert_delivery` |

### 20.5 Supervision layers — 🟡 (C: restart, dead man's switch, second channel)

| Layer | Today |
| --- | --- |
| In-process supervisor tree | 🟡 Stops a known managed process and checks for a new heartbeat. Live/testnet restart is blocked: independently proving venue state and stopping an unmanaged process remain unresolved. |
| External watchdog process | 🟡 heartbeat, kill-file, L2 escalation and alerting exist. No CLI venue client; live/testnet restart is blocked until independent venue state and a safe preflight are verified. |
| Container restart policy + health checks | ✅ `docker-compose.yml` + `/healthz` |
| Dead man's switch (live) | 🟡 File heartbeat and latch exist. The CLI has no venue client, and existing adapters do not provide an independently verified complete venue snapshot, so automatic flattening is not established. |
| Independent alert channel | ✅ C: file floor + Telegram + a generic webhook (`ALERT_WEBHOOK_URL`). `router.independent` is False until two remote transports are configured, and `tradebot health` says which ones are |

### 20.6 Testing and measuring — 🟡

| Requirement | Today |
| --- | --- |
| One test per playbook row | 🟡 B covers all six wired rows (`tests/integration/test_heal_engine.py`) and the machinery (`tests/unit/test_heal.py`); the unwired rows in the table above cannot be tested because they do not exist yet |
| Injection tests fail first, then get repaired | ✅ B: the stale-feed test asserts the repair *cannot* verify in replay and that escalation follows; the reconcile and stuck-exit tests assert the repair verifies |
| MTTR targets (feed <10 s, worker <5 s, restart+reconcile <60 s) | ✅ per-row `mttr_seconds` recorded on every outcome, compared with the row's target and printed by `tradebot health` |
| Monthly game day in paper mode | ⬜ (the pieces it would exercise now exist: `tradebot watchdog --once`, `tradebot alerts --test`, `tradebot drill`) |
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
| 21.7 | Autonomy limits, approvals, autonomy ledger, forbidden-action tests | 🟡 working branch has partial ledger and approval records; request snapshots, decision events and ledger events now commit atomically. Identity authentication, action-bound consumption and enforcement at actual mutation boundaries remain unbuilt; old rows may lack audit events. |
| 21.8 | Operator-absent ladder (7 d warn, 14 d half size, 21 d L1) | ⬜ |
| 21.9 | Build order: operate + heal first | ✅ this file follows it |
| 21.10 | Honest limits + scenarios 29–34 | 🟡 stated in docs; the mechanisms are the rows above |
| 21.11 | Dashboard Health + Autonomy + go/no-go additions | 🟡 working branch adds read-only health and partial-autonomy audit panels and `/api/autonomy`; complete audit coverage, approval authorization, go/no-go and preview access remain open |

---

## Build order

| PR | Scope | State |
| --- | --- | --- |
| **A** | Health model, component checks, `tradebot health`, `/api/health`, report section, restart budget for strategies, audit events | ✅ merged |
| **B** | Heal playbook table + circuit breaker (60 s, half-open probe) + stuck-**exit** sweep + verification, cooldowns, escalation + MTTR + one injection test per wired row | ✅ merged |
| **C** | Watchdog v2: restart budget, dead man's switch and alert router | 🟡 merged implementation; live venue preflight is not verified and live restart is blocked |
| **D** | Autonomy ledger + approval flow + forbidden-action tests (§21.7) + flapping forces L1 | 🟡 in progress; order-manager and CCXT entry checks, persisted software stop ratchet, fail-closed live ramp and critical-incident accounting and escalation outbox, L1 recovery identity checks and race-safe remote reset checks added; global boundaries and authenticated approvals remain |
| **E** | Market graph (§21.2) and the contagion exposure cut | ⬜ |
| **F** | Allocator (§21.3) with the equal-weight backtest comparison as its gate | ⬜ |
| **G** | Lifecycle manager (§21.4) with decay auto-disable | ⬜ |
| **H** | Fund loop (§21.5), operator-absent ladder (§21.8), weekly digest | ⬜ |
| **I** | Dashboard Health + Autonomy pages, MTTR metrics, monthly game day | 🟡 partial read-only panels on working branch; game day and go/no-go remain |

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
  keep-last-good and exchange status polling are listed as ⬜ above rather than being
  claimed by a table entry. (The alert retry queue was one of them, and C built it.)

### What PR C deliberately did not do

- **Be the outer supervisor.** `ProcessSpawner` starts a child of the watchdog; it cannot
  outlive its own parent. Surviving a reboot needs systemd or a Docker restart policy, and
  the module says so instead of implying otherwise.
- **Give the watchdog a venue client in the CLI.** `tradebot watchdog --supervise` reports
  `venue_client: none`, because building a ccxt client from the CLI is not wired yet. It can
  escalate and alert, but cannot sell. Live/testnet restart is blocked until an independent
  venue snapshot and verified preflight are implemented; paper restart remains available.
- **Touch risk limits.** The watchdog still only raises kill levels, never lowers them, and
  the dead man's switch only closes positions.
- If a mechanism cannot be verified here, it is marked 🔒 rather than claimed.

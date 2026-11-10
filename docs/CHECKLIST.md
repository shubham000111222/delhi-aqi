# Completion checklist

The project still needs both implementation and external evidence. This file is the
one place that says what is left, in order, with the exact command, the pass criterion, and
what to send back. Work down it top to bottom; each phase produces a number the next phase
depends on.

*Status legend:* ✅ done · ▶ next · ⬜ not started · 🔒 blocked on something only you can do.

> **What "complete" means here.** Every component built, every external integration
> verified against the real thing, and an honest verdict on real recorded data. It does
> **not** mean "profitable": the acceptance gates are built to say no, and on synthetic
> data they already do. A project that reports its own failure is finished; one that
> manufactures a pass is not.

For the full Windows installation, implementation and validation procedure, see
[`WINDOWS_LOCAL_LIVE_READINESS.md`](WINDOWS_LOCAL_LIVE_READINESS.md).

**Last updated:** 2026-11-04 · **Suite:** 962 passed, 2 skipped on the working branch.
The previously reported archive (30 days × 5 symbols, 216,198 bars) was on the user's
machine; this workspace has no real-price archive.

---

## Phase 0 — code that was still open

| | Item | Evidence |
| --- | --- | --- |
| ✅ | **Backfill tail misread as `STUCK`.** A window ending at a wall-clock instant (`21:52:05`) ends while the `21:52` bar is forming; that page moved no cursor, so the "exchange is repeating itself" guard fired on a perfect 30-day fetch. Completeness was then computed against `end`, failing by five seconds. | `expected` now excludes the bar that had not closed; a page of only unclosed rows means end-of-data, not `stuck`; three regression tests reproduce the real numbers (45 pages, 43,201 fetched, 43,199 kept, 2 unclosed) |
| ✅ | **`backfill-history`.** Filling six months is six chances to lose a page; it now chunks on month boundaries, writes each chunk as it arrives, and resumes from a state file. | `tradebot backfill-history --months 6`; a truncated chunk exits 4 and is retried, a complete one is skipped. Five tests. |
| ✅ | **`--feed archive`** — recorded candles replay through the real engine, so the whole validation suite runs on the tape instead of a simulation. | `docs/RESULTS.md` → *Replaying the archive* |

---

## Phase 1 — data depth ▶ **start here**

The gates need ≈200 closed trades before they mean anything. Your 30-day archive produced
single-digit trade counts on an equivalent tape, so the archive is the bottleneck.

- [ ] **1.1** Extend the archive to six months:

```powershell
python -m tradebot backfill-history --months 6 --page-limit 1000 --json > history.json
```

  Expect ~15 minutes per symbol-month. It is safe to interrupt: rerun the same command and
  it continues at the first chunk that is not complete.

- [ ] **1.2** Confirm what landed:

```powershell
python -m tradebot archive
```

  **Pass:** 5 symbols, approximately six calendar months of complete one-minute bars,
  and one `complete` line per chunk in `history.json`; verify the actual date range and gaps.

- [ ] **1.3** If a chunk says `incomplete` or exits 4:

```powershell
python -m tradebot backfill-history --months 6 --page-limit 1000 --retry-incomplete
```

  **Never** paper over a gap by widening `--days`: a hole in the archive is a hole in every
  indicator computed across it.

- [ ] **1.4** Send back: the `kept_total`, `gap_total` and `complete` fields from
  `history.json`, plus `python -m tradebot archive` output.

---

## Phase 2 — the verdict on real prices

Run on the archive, not on a simulation. `--feed archive` reads `data/archive` by default.

- [ ] **2.1** `python -m tradebot backtest --feed archive --days 180` *(10–20 min)*
- [ ] **2.2** `python -m tradebot walk-forward --feed archive --train-days 60 --test-days 30 --windows 4`
- [ ] **2.3** `python -m tradebot robustness --feed archive --days 180 --cost-multiplier 2`
- [ ] **2.4** `python -m tradebot signals --feed archive --days 180 --horizon 4`
- [ ] **2.5** `python -m tradebot sensitivity --feed archive --strategy S3_breakout --param donchian=10,20,30,40 --days 120`

**Pass** (§8 gates, all of them): OOS profit factor > 1.3 · Sharpe > 1.0 · max drawdown
< 15% · ≥ 200 closed trades · still profitable at 2× costs · Monte-Carlo p95 drawdown
< 20%. Promotion also needs a ~10% net-expectancy edge across ≥ 3 windows (§19.2).

**Expected outcome:** a fail, and that is information, not a defect. Read *which* gate
fails: `trade_count` means "not enough history" (go back to Phase 1); `out_of_sample_profit_factor`
or the 2× cost rerun means fees and turnover are eating the edge.

- [ ] **2.6** Send back all five outputs. Then either Phase 3 (a pass) or a code cycle:
  lower-turnover / maker-first entries, and Phase 2 again.

---

## Phase 3 — measure the spread the replay has to assume

A replay's depth is **modelled** from each bar close (the archive holds no order books), and
every archive result prints that caveat. Only a live session replaces the assumption with a
measurement.

- [ ] **3.1** Start the soak, machine awake, wired network:

```powershell
python -m tradebot run --feed ccxt --minutes 43200 --mlflow mlruns > soak.log 2>&1
```

- [ ] **3.2** Daily: `python -m tradebot status`, and the dashboard at `http://localhost:8000`.
- [ ] **3.3** After ≥ 30 live fills: `python -m tradebot slippage --days 30`
  → compare measured p95 with the modelled 1.5 bp half-spread.
- [ ] **3.4** `python -m tradebot metrics` → gates on the paper database, not a backtest.
- [ ] **3.5** `python -m tradebot learn --db data/state/tradebot_paper.db`
  → writes a veto card only if it earned one; prints "NEED_MORE_DATA" otherwise.
- [ ] **3.6** `python -m tradebot meta --days 30` → does a meta-label gate earn its place.
- [ ] **3.7** `python -m tradebot drill` → records time-to-flat.
- [ ] **3.8** Send back `slippage` and `metrics`.

---

## Phase 4 — integrations only your machine can verify

| | Item | Command | Pass |
| --- | --- | --- | --- |
| 🔒 | Telegram | configure `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` locally using `.env.example`, then run `python -m tradebot telegram` | prints reachable, message arrives |
| 🔒 | News overlay (optional) | `python -m tradebot news --url <rss>` | multiplier ≤ 1.0, never grows a position |
| 🔒 | MLflow (optional) | `mlflow server --port 5000` then `python -m tradebot experiments --read-server http://127.0.0.1:5000` | runs visible |
| 🔒 | NSE / Kite (optional) | `python -m tradebot venues --venue nse --notional 100000` with keys | session + cost model printed |
| 🔒 | Docker (optional) | `docker compose up` | dashboard on :8000 |

---

## Phase 5 — testnet, then the live ramp

Only after Phase 2 and Phase 3 pass. Live mode is refused unless every guard in
`risk/live_guards.py` holds, and the capital cap stays at 250 USDT.

- [ ] **5.1** Configure testnet keys and validate the testnet adapter. `live-check` is a live-mode check, not a testnet certification; it requires a confirmation argument when live mode is eventually assessed.
- [ ] **5.2** Two weeks on testnet; re-run `slippage`, re-train the ledger card on testnet trades.
- [ ] **5.3** Only after the safety gaps and preceding gates are resolved: `python -m tradebot live-check --mode live --confirm "250 LIVE" --with-adapter` (replace 250 with the configured capital cap).
- [ ] **5.4** First live week supervised: `status` twice a day, `drill` once, ramp enforced.
- [ ] **5.5** Promote only while `metrics` keeps passing; any failed gate drops back to paper.

---

## Phase 6 — self-healing and self-sustaining (build plan §20–21)

Tracked in detail in [`BUILD_PLAN_20_21.md`](BUILD_PLAN_20_21.md); this is the reading order.

- [x] **Health model** — every component reports liveness / readiness / correctness;
      `tradebot health`, `/api/health`, and the daily report all read the same snapshot.
- [x] **Restart budget** — three faults an hour, then the strategy is disabled and escalated.
- [x] **Heal playbook core** — six rows wired end to end (stale feed, API pressure + 60 s
      circuit breaker, stuck *exit* re-submitted at market, unprotected position gets its
      stop, disk purge, ledger/adapter reconciliation). Every action is verified; an
      unverified repair counts as a failure; three failures in ten minutes escalate once.
      MTTR per row is compared with the §20.6 target and printed by `tradebot health`.
- [ ] **Heal playbook, remaining rows** — candle repair, memory restart, clock resync,
      config keep-last-good, alert retry queue, exchange status polling, model rollback.
- [ ] **Watchdog v2** — restart budget, new-heartbeat verification, latching dead man's switch and alert routing exist. Live/testnet restart is blocked pending an independently
      verified venue preflight; automatic flattening is not available through the CLI.
      `tradebot watchdog --once`, `tradebot alerts --test`.
- [ ] **Watchdog venue safety verification** — audit stop quantity/side and verify actual
      executions before restarting; a submitted or partially covering stop is not proof that
      an entire position is protected or flat. The CLI currently has no venue client.
- [ ] **Watchdog v2, the part a host must do** — `ProcessSpawner` starts a child of the
      watchdog, so surviving a reboot needs systemd/Docker. Until then, supervision dies with
      its parent and says so.
- [ ] **Autonomy ledger + approvals + forbidden-action tests** (§21.7): initial audit table and approval records exist on the working branch. Request snapshots, decision events and autonomy ledger events now have transactional audit coverage; approval decisions remain unverified, and historical decisions may lack those events. Integrate every decision path, extend authenticated, action-bound approvals beyond tracking-fault acknowledgement and risk-reducing allocation cap changes to actual allocation increases and reset authorization, and enforce/test actual mutation boundaries before treating this as complete. The refusal helpers alone do not enforce policy. Risk limits and the outer config binding are frozen; absolute safety-envelope values are no longer configurable, and risk entry, order-manager and CCXT entry paths check runtime risk/mode/adapter changes. Reduce-only buys are rejected at the order manager. This is not isolation from arbitrary Python/SQL, raw exchange access, or all other adapter paths. Normal live entries now carry their risk-sizing multiplier to the order manager and CCXT adapter, which recheck it against the current cap immediately before placement; this is a caller assertion, not proof of the computed quantity, and the cap may still change after the final check. A signed one-use operator action can now lower the durable live allocation cap used by normal sizing; it cannot raise or clear that cap, reset the kill switch or certify live readiness. See [signed recovery](SIGNED_RECOVERY.md). Live ramp advancement now refuses without an authenticated increase approval consumer, and legacy ramp stages cannot exceed the initial 10% live sizing cap (a stricter configured initial cap is honored). Direct ramp saves cannot raise stage or erase incidents; incident increments and audit events are atomic. Recorded L2/L3/CRIT incidents now atomically update the ramp counter and audit events; a corrupt ramp leaves a durable invalid marker and blocks live sizing. Kill-switch and watchdog escalation now persist a generation-bound incident outbox in the kill transaction; pending records block entry/reset and replay on kill-switch initialization. A failed outbox write retries escalation with a persistent tracking-fault hold, and incident-write failure no longer interrupts the caller's protective action. A signed, one-use offline acknowledgement can clear the fallback fault without lowering the kill level, but it replaces it with an unverified-incident hold that has no authenticated reconciliation/clearing workflow; database failure can still prevent even the fallback escalation, and direct store calls can bypass this mechanism. See [signed recovery](SIGNED_RECOVERY.md) for trust assumptions and the paper-only Windows workflow. Other unrecorded incidents, historical incident backfill, raw database changes and alternative sizing/execution paths remain outside these checks. Fault-disabled strategies are now latched in SQLite with an atomic autonomy row and event, survive restart, and are rejected at normal order-manager and CCXT entry paths while exits remain available. Fault attempts and their one-hour pauses now persist transactionally across restarts and concurrent engines; the fourth fault latches a disable. Historical disables and pre-existing in-memory fault budgets are not migrated, and there is no authenticated re-enable workflow; strategy names can be misrepresented by other callers and raw exchange/SQL access bypasses the boundary. The software trailing-stop ratchet is persisted transactionally with its audit event; stale `save_position` updates retain stronger persisted stops while preserving fill-accounting updates. This does not update or verify a venue stop, protect against direct SQL, or enforce every in-memory mutation. The kill-switch auto-resume boundary now restricts eligible causes and binds recovery to the evaluated transition; manual resets also reject a present KILL file, fail closed on invalid L3 cooldowns or corrupt flapping holds, and bind Telegram's L1 review to the reset transition. Critical flapping hold extensions are serialized and cannot shorten an existing deadline; entries also refuse a durable active or corrupt hold even before L1 is persisted. This does not stop orders already in flight or cover raw exchange paths. Kill-switch construction now requires an explicit mode, and order-manager/CCXT entries reject mismatched or replaced switches. Manual and automatic reset in normal live/testnet engine and CLI kill-switch instances now refuse entirely until authenticated reset and independent recovery evidence are implemented. Paper retains the existing reset path. The complete independent post-heal checklist and authentication/authorization of manual reset remain open; a filesystem KILL file can appear after the check and direct store writes bypass this boundary. The order manager now re-checks the persisted kill level immediately before each non-reduce-only placement and refuses unknown-outcome retries, with boundary tests. The CCXT adapter now also fails closed for direct non-reducing submissions without an attached kill switch, checks persisted kill state and rejects a buy labelled reduce-only; dashboard remote resume is L1-only and bound to its reviewed state. This is not global enforcement: raw exchange calls, custom/injected and other adapters, in-flight requests, and independently proving sell reduce-only venue behavior remain unresolved.
- [x] **Flapping**: critical flapping holds L1, persists its deadline and recent health transitions across restart, and blocks automatic or manual L0 reset until the window settles.
- [ ] **Market graph** (§21.2) — persistent asset/strategy/source relationships, lead-lag,
      beta and contagion cuts; test against recorded data.
- [ ] **Allocator** (§21.3) — volatility/performance tilt and bounds; ship a non-equal
      allocator only if it beats equal weighting after costs in backtest.
- [ ] **Strategy lifecycle** (§21.4) — nine states, gated transitions, decay disable and retirement.
- [ ] **Fund loop** (§21.5) — cost ledger, self-funding ratio, break-even, reserves and budgets.
- [ ] **Learn schedule** (§21.6) — drift, retrain, research and audit schedules without automatic promotion.
- [ ] **Operator-absent ladder** (§21.8) — 7 days warn, 14 days half size, 21 days L1.
- [ ] **Dashboard Health/Autonomy** (§21.11) — working branch has read-only health and
      partial-audit panels, but approval authorization, full audit coverage and go/no-go remain.
- [ ] **Historical backfill duration** — fix `--months` arithmetic for 12+ months and
      test year and leap-day boundaries; until fixed, use explicit UTC `--from`/`--to`.
- [ ] **Promotion evidence integration** — provenance is not verified by the current
      metrics pipeline, and the 2×-cost rerun is not connected to its verdict; promotion
      fails closed until both are supplied and independently validated.
- [ ] **Dashboard exposure** — controls default to disabled and require a password if
      enabled; read endpoints are unauthenticated and require a protected deployment.
- [ ] **Game day and MTTR evidence** (§20.6) — exercise every playbook row and measure targets.
- [ ] **Arena preview access** — port 8000 responds locally, but the preview gateway was
      inaccessible to the user; requires platform-side investigation.

## Phase 7 — housekeeping (monthly)

- [ ] `python -m tradebot experiments --export mlruns-export` — the multi-testing audit trail.
- [ ] `python -m tradebot archive --keep-days 90` — prune book partitions, keep candles.
- [ ] Back up `data/state/*.db` and `data/archive/` — both are gitignored, nothing else protects them.
- [ ] Record every durable number in `docs/RESULTS.md`; it is the project's memory.

---

## Decisions only you can make

These change results, so decide before Phase 5.

- [ ] Quote currency: USDT (default) · USDC · INR pairs
- [ ] Coin list: 5 today; fewer coins = fewer fees, less diversification
- [ ] Capital: 1,000 USDT paper default, 250 USDT live cap until a soak passes
- [ ] Hosting: this PC (no sleep, wired) or a small VPS before live
- [ ] Whether Telegram / LLM journaling are wanted at all (both are optional fallbacks)

---

## Results log

Fill this in as phases complete — it is what `docs/RESULTS.md` is built from.

| Date | Phase | Command | Result | Verdict |
| --- | --- | --- | --- | --- |
| 2026-11-03 | 1 | `backfill --days 30 --write` | 5 symbols, 216,198 bars, gaps 0, coverage 1.0 | complete (2 symbols mislabelled `STUCK` — fixed in Phase 0) |
|  |  |  |  |  |

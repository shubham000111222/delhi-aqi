# Windows 11: local implementation and live-readiness procedure

**Status: work in progress, not a live-trading authorization.** This is the procedure for a developer/operator to take the existing repository from its current paper-first state toward a reviewed, live-ready system. It distinguishes runnable commands from code that still has to be implemented. Nothing in this guide guarantees profit or safety. Do not put real money at risk until every applicable stop condition, review and sign-off below is satisfied.

**Last checked:** 2026-11-04, against the Arena working tree. The most recent local audit there ran 758 tests successfully, with two skipped; that does not certify your Windows copy or any external service.

**Important version warning:** The latest audit fixes and this guide belong to the combined working branch `arena/200089ad-delhi-aqi`, not necessarily `main`. Your Windows checkout does **not** gain these changes merely by pulling `main` before the combined work is merged. Obtain and review the branch changes before relying on commands or behavior that depend on them. Do not reset, overwrite or delete your existing Windows work or market data to obtain it. If your local checkout differs, first inspect `git status` and coordinate the transfer of the changes.

Other references: `docs/CHECKLIST.md`, `docs/BUILD_PLAN_20_21.md`, `docs/REPO_AUDIT.md`, `docs/RESULTS.md`, `docs/GO_NO_GO.md` and `docs/RUNBOOK.md`. Some older documentation describes intended behavior or has examples that predate this audit. In particular, the current `live-check` does not establish a successful live go/no-go, `slippage` runs a new simulated/replay session rather than reading your existing paper session, and the CLI `walk-forward` does not refit parameters. Prefer the actual code and this document's explicit caveats. Record any discrepancies and fix them before deployment.

## How to use this guide

- Work phases in order. **Part A** is paper-only and can run before the missing engineering is completed. **Part B** is the developer implementation and verification work. **Part C** requires real-world evidence and independent review. Part B and Part C are not completed by running a command.
- Every box is a task, not evidence by itself. Record the exact code version, config, data dates, commands, result and reviewer in `docs/RESULTS.md` or a private operations log. Do not put credentials or private account information into Git.
- Commands below are PowerShell for Windows 11. Run them from the repository root in an activated virtual environment. Do not run two engine instances against the same trading database.
- Keep live execution disabled throughout implementation. Do not bypass guards, forge a go/no-go record, manually alter the database to make a gate pass, or supply invented timestamps or venue state.
- Running tests, processing events, a rising simulated equity curve, a dashboard HTTP response or a `MATCH` with zero decisions are **not** evidence of profitable or safe live execution.

## Phase 0 — establish ownership, version and a recovery copy

1. [ ] Decide who will implement and independently review the code and who will operate it. If you are not comfortable with trading-system development and exchange APIs, use a qualified developer/reviewer. Only you can choose the exchange, capital at risk, and whether to trade.
2. [ ] Install Git and Python 3.11 on Windows 11. Confirm `python --version` reports 3.11.x. Install adequate disk space and a stable network; determine whether the selected exchange, symbols and API are actually available to you in India.
3. [ ] Inspect your existing checkout before changing it:

```powershell
cd C:\Users\shubh\Downloads\trad\delhi-aqi
git status -sb
git log -1 --oneline
python --version
```

4. [ ] Preserve uncommitted changes, `config/`, `.env`, `data/`, reports and logs. The data, SQLite databases, `.env` and model cards are Git-ignored; GitHub cannot restore them. Do not upload your `.env` or secrets to GitHub. Transfer or merge the latest Arena work only after it is published and you understand the differences. Record the actual commit hash that you run.
5. [ ] Back up before each update and before destructive experiments. For SQLite, use its online backup API rather than copying only a `.db` while WAL writes are active. For example, after confirming the path is correct, this backs up all top-level state `.db` files to a timestamped folder in your Windows profile:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$env:TRADEBOT_BACKUP_DIR = Join-Path $env:USERPROFILE "TradebotBackups\$stamp"
New-Item -ItemType Directory -Force -Path $env:TRADEBOT_BACKUP_DIR | Out-Null
@'
import os, sqlite3
from pathlib import Path
source_dir = Path('data/state')
dest_dir = Path(os.environ['TRADEBOT_BACKUP_DIR'])
for path in source_dir.glob('*.db'):
    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    destination = sqlite3.connect(dest_dir / path.name)
    try:
        source.backup(destination)
        print('Backed up:', path.name)
    finally:
        destination.close()
        source.close()
'@ | python -
```

Back up the archive and non-secret config separately to a suitable private location, subject to available disk space. Store secrets separately and securely; never paste them into logs or a public issue. Periodically restore a backup into a **separate test directory**, run `PRAGMA integrity_check`, and check that expected data exists. A backup is not proven until restore is tested. Stop the bot before any real restore; never overwrite an active database.

## Phase 1 — install and validate the baseline

1. [ ] In PowerShell, from the repo root, use your existing `.venv` or create one. The activation command affects only the current PowerShell window:

```powershell
cd C:\Users\shubh\Downloads\trad\delhi-aqi
# Only if .venv does not already exist:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
```

If activation is blocked, use `.\.venv\Scripts\python.exe` in place of `python`; do not weaken machine-wide execution policy simply to run this project.

2. [ ] Run the checks and save results:

```powershell
python -m tradebot check
python -m pytest -rs
ruff check src tests
mypy src
git diff --check
```

Inspect failures **and skips**; fix unexpected ones. Check `mode`, `feed`, database path, symbols, capital and redacted secrets output from `check`. Never assume your local tests exactly match the Arena count. A zero exit code from a backtest command is not the same as a positive strategy verdict.

3. [ ] Install only the version of the latest work you have reviewed. If tests depend on external software, record versions and availability. A wheel and Docker configuration were inspected in Arena; Docker was not actually run there. Local Docker testing is a separate task below.

## Phase 2 — configure a safe paper environment

1. [ ] If `.env` is absent, create it without overwriting an existing file:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

Keep `LIVE_TRADING_ENABLED=false`, `DASHBOARD_READONLY=true` and live/testnet keys empty for now. The environment overrides `.env`; inspect any variables you have intentionally set without printing their secret values. Do not share screenshots of the file.
2. [ ] Review `config/settings.yaml`, `config/risk.yaml`, `config/strategies.yaml` and `config/symbols.yaml`. Keep `mode: paper` and use a deliberate paper starting balance. Check the selected spot symbols, fees, position limits, loss thresholds, directories, exchange ID and time zone. Do not enlarge hard risk envelopes to make a test pass. `config.settings.max_capital_usdt` is not proof that every live venue path has been independently verified to enforce the intended cap.
3. [ ] Choose separate isolated data directories/configurations for synthetic experiments, real-price paper, testnet and any future live deployment. Never run a backtest with `--persist` against a real paper or live database. Record which database each command uses. Avoid multiple engine processes sharing the same database.

## Phase 3 — synthetic paper smoke test and dashboard

1. [ ] Start one paper/synthetic demo at real-time speed, bound only to this PC:

```powershell
python -m tradebot dashboard --mode paper --feed synthetic --speed 1 --host 127.0.0.1 --port 8000
```

2. [ ] On the same PC open `http://127.0.0.1:8000`. Check that the page refreshes, mode is paper, controls are disabled and timestamps advance at about 1×. If port 8000 is occupied, inspect before acting:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess
```

Do not kill an unknown process. Stop the demo with `Ctrl+C` when finished. The hosted demo only supports paper plus synthetic feed. A fresh process may initially show degraded feed while waiting for a complete batch. Check individual components as well as the overall health.
3. [ ] Recognize the limitations: synthetic market data, simulated order execution, and paper equity are not real exchange data or profitability. No trades can simply mean no strategy signals. Read endpoints, including reports, are not authenticated. Keep the dashboard on loopback or behind a separately authenticated, HTTPS-protected proxy. If controls are ever explicitly enabled, the password protects control requests only, not read access; do not enter a password over an untrusted HTTP network.
4. [ ] A `dashboard --no-bot` process has no attached engine. Its stored-state snapshot is not an authoritative live position or component-health view, and its controls cannot operate a separate engine. Do not interpret its empty positions as proof the exchange or other process is flat.

## Phase 4 — acquire and check real historical data

1. [ ] The repository's `docs/GO_NO_GO.md` asks for at least two years of real exchange history for the strategies, spanning differing regimes. Decide the exchange, symbols, time frame, exact UTC start/end, and untouched holdout **before** research; confirm exchange access and history availability. Two years of one-minute data can require substantial time, disk and rate-limit budget. A six-month archive is a preliminary step, not the full two-year target.
2. [ ] The current `backfill-history --months` calculation is not reliable for 24 months: it subtracts at most one calendar year. Use explicit `--from` and `--to` instead. The example below uses midnight UTC today as a fixed end; save the range so retries use exactly the same timestamps. It does not promise that the exchange actually has all the data.

```powershell
New-Item -ItemType Directory -Force -Path logs | Out-Null
$endUtc = [datetime]::UtcNow.Date
$startUtc = $endUtc.AddYears(-2)
$range = @(
    $startUtc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
    $endUtc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
)
$range | Set-Content -Encoding utf8 logs\backfill-range.txt
Get-Content logs\backfill-range.txt
```

After checking the dates, run:

```powershell
$range = Get-Content logs\backfill-range.txt
python -m tradebot backfill-history --mode paper --source ccxt --from $range[0] --to $range[1] --page-limit 1000
$LASTEXITCODE
python -m tradebot archive
```

3. [ ] Verify every configured symbol's date range, closed-bar coverage, duplicates, missing intervals and incomplete chunks using the output and `data/backfill-history.json`. Keep the same endpoints and state file when resuming. A nonzero exit/incomplete chunk means investigate; never fill holes with invented values. `--retry-incomplete` currently causes even previously complete chunks to be fetched again; do not assume it is selective. Back up the archive and state before retries. Exchange throttling, unavailable symbols/history, outages and incomplete candles need explicit handling.
4. [ ] Record source exchange, download time, exact interval, symbol list, data hashes, gaps and any exclusions. Keep the final holdout physically or procedurally separate from all development, tuning, feature selection and model training. Restrict access and maintain an audit of any holdout use. The diagnostic CLI commands below read their configured archive; they do not enforce a holdout boundary. Before running them, point them at a separately prepared development-only archive and verify its contents and date range. Do not simply run them against an archive that includes the untouched holdout.

## Phase 5 — exploratory validation; do not call it promotion

1. [ ] Before the engineering in Phase 8 is complete, the following commands are useful diagnostics only. They may take a long time. They replay from available archive bounds and may clip to available data. Inspect reported start/end, effective windows, gap coverage and actual trade count; requesting 180 days does not prove 180 complete days were processed.

```powershell
python -m tradebot backtest --mode paper --feed archive --days 180
python -m tradebot walk-forward --mode paper --feed archive --train-days 60 --test-days 30 --windows 4
python -m tradebot robustness --mode paper --feed archive --days 180 --cost-multiplier 2
python -m tradebot signals --mode paper --feed archive --days 180 --horizon 4
python -m tradebot sensitivity --mode paper --feed archive --strategy S3_breakout --param "donchian=10,20,30,40" --days 120
```

2. [ ] Record the entire output and all unsuccessful or skipped research attempts. Protect the holdout; do not repeatedly tweak parameters until it passes. Examine fees, fill assumptions, exposure, regime breakdown, sample size, trade counts and the full equity path. Check that robustness actually ran the same strategy and period with doubled costs; the CLI's nominal gate verdict does not yet consume that evidence.
3. [ ] Known limitations: the CLI walk-forward currently runs independent test windows without a training/refit callback; archive depth is modeled from candle closes; the current promotion gate intentionally fails OOS provenance because the pipeline cannot verify it, and cost-stress evidence is not wired into the verdict. `tradebot parity` as currently wired records a synthetic session; a zero-decision match proves no execution behavior. `tradebot slippage` runs a new session; it is **not** a report on your existing real-price paper database. All these limitations require implementation or separate verification, not a configuration workaround.
4. [ ] Formal requirements before promotion (see `docs/GO_NO_GO.md`): OOS profit factor strictly > 1.3 after costs; Sharpe > 1.0; maximum drawdown < 15%; at least 200 closed OOS trades; profitable after an independently verified 2×-cost rerun; Monte Carlo p95 drawdown < 20%. Assess multiple testing, parameter plateau, leakage, regime dependence and a genuinely untouched holdout. The learned-card requirement is approximately 10% net expectancy improvement across at least three windows (§19.2), as well as its other gates. Missing or invalid evidence means **NO GO**, even if a command prints a favorable number.
5. [ ] No favorable result is guaranteed. If it fails, preserve the failure, reconsider the strategy, and use a new, properly separated validation protocol. Do not lower requirements or silently reuse the holdout.

## Phase 6 — short live-price paper test, then a 30-day soak

1. [ ] Stop the synthetic demo before using the same database. Confirm the config still says paper and the selected public market-data exchange/symbols are reachable. Start a bounded paper session:

```powershell
python -m tradebot run --mode paper --feed ccxt --minutes 60
```

This reads real market prices through CCXT REST polling but uses the simulated paper adapter, not real orders. Check timestamps, feed errors, gaps, rejected data and that the process stays healthy. If the exchange is unreachable or symbols are unsupported, stop and investigate; do not switch silently to synthetic and call it a live-price soak.
2. [ ] After a clean short test and a safe independent deployment design, conduct 30 consecutive days of paper trading on live prices with continuous monitoring. Keep the PC awake, secure and online, provide appropriate power/network redundancy, and measure time synchronization (`w32tm /query /status`). Have a human review every day. A Windows task scheduler, Docker or system service must be explicitly configured, tested and supervised before relying on restart after a reboot; no sample command here certifies it.
3. [ ] Check daily: process and feed timestamps, each component's state, positions/orders from the paper system, kill history, errors, reconciliation, fills and slippage assumptions, daily report, backups, disk, network, time sync and alerts. Use:

```powershell
python -m tradebot status --mode paper
python -m tradebot metrics --mode paper
Get-Content logs\tradebot.log -Tail 50
```

Know that `metrics` now correctly reports missing provenance/cost evidence rather than proving promotion. The paper dashboard without an attached engine does not replace independent monitoring. Record downtime and incidents; the go/no-go checklist calls for zero unhandled exceptions and zero reconciliation mismatches, with a rehearsed kill drill and rollback. Investigate and repeat the soak as needed rather than ignoring incidents.
4. [ ] Treat simulated paper fills as simulations. Implement and validate an auditable way to measure decision price, order-book state, latency and real/testnet executions before claiming measured live slippage. Venue-specific behavior and insufficient samples must remain visible.

## Phase 7 — configure alerts and exercise incident response

1. [ ] If desired, configure `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` and an independent `ALERT_WEBHOOK_URL` in `.env`. Do not print or share them. Verify that your endpoint actually accepts this implementation's JSON payload (`text` and `level`); not every webhook service has the same protocol. Test in paper mode:

```powershell
python -m tradebot alerts --test --mode paper
```

2. [ ] Confirm a message actually arrives on the intended phone/channel; inspect results for each transport. A successful write to disk or a configured URL is not proof of delivery. Test the alternate channel independently, simulate failures in paper, exercise the retry queue and verify warnings reach a human. Never rely on a channel that was only configured, not tested.
3. [ ] In a safe isolated paper environment, inject stale feed, order/API failures, disk pressure, unavailable alerts, hung process and other implemented playbook faults. Record initial failure, action, verification, escalation, timing and outcome. Check that no healer increases exposure, lowers kill levels or edits risk parameters. Run each scenario with failures of the repair itself; verify it fails closed. Never perform destructive fault injection against a real account.
4. [ ] Emergency procedure: stop adding risk; check bot status and logs; if reachable issue the appropriate stop through the reviewed operator path; independently open the exchange account to cancel orders and close positions if needed; revoke compromised keys; document the incident; do not resume until reconciliation and a human review are complete. Stopping the process or container does not cancel exchange orders. The existing `tradebot drill` is a paper simulation and must be inspected for an actual position before its time-to-flat result is considered meaningful.

# Part B — implement the missing software before testnet/live

## Phase 8 — complete safety foundations first

Work on the existing codebase and keep changes/tests in the same combined D–I workstream. For each item: write a failing test, implement the smallest bounded fix, test the actual mutation boundary and failure modes, run targeted checks, then the full suite. Update `docs/CHECKLIST.md` and `docs/BUILD_PLAN_20_21.md` with evidence; never mark an untested integration complete.

### 8A. Approval, authorization and autonomy boundaries (D)

- [ ] Inspect `src/tradebot/autonomy.py`, `core/store.py`, `engine.py`, `risk/killswitch.py`, the dashboard/Telegram/CLI controls, watchdog and all order/config mutation paths. The existing ledger and approval records are partial: a caller-provided actor or approver name is not authentication and helper-level refusal is not enforcement.
- [ ] Design operator authentication and authorization with a real trust boundary. Bind approval to exact action, target, parameters, requester, authenticated approver and expiry; forbid self-approval where required. Persist immutable audit records for request, decision, denial, consumption and execution. Handle clock skew, expiration, replay, duplicate requests and process restarts.
- [ ] Atomically consume approval with local database mutations; fail closed on expired, malformed, reused, mismatched or unauthenticated approvals. Exchange requests cannot share a SQLite transaction: use a durable authorized intent, stable idempotency key, and venue reconciliation to handle crashes or ambiguous responses before retrying. Prove the order cannot be submitted without a consumed action-bound authorization. Do not grant arbitrary execution via an approval token.
- [ ] Enforce safety where state actually changes: models/healers cannot raise risk limits, increase exposure, widen protection, re-enable disabled strategies or lower kill level. Use the narrowly bounded, infrastructure-only L1 auto-resume exception only after verified health and reconciliation. Verify no direct adapter/store/CLI path bypasses the policy. Validate concurrent process races against a real SQLite database and, for venue actions, an authoritative venue state.
- [ ] Flapping requirement: more than five critical component changes within ten minutes must hold L1 until settled, including across restart, while manual and auto L0 resets are blocked. Test corruption, timing, competing resets and concurrent escalation.
- [ ] Add adversarial tests for impersonation, changed parameters, duplicate/replayed decisions, TTL boundaries, direct mutation, racing L3 escalation and all forbidden actions. Audit every attempt, including refusals, without recording secrets.

### 8B. Live preflight, reconciliation and watchdog venue safety

- [ ] Inspect `risk/live_guards.py`, `main.py`, `execution/adapters/ccxt_live.py`, `watchdog/watchdog.py` and `watchdog/supervisor.py`. Today `cmd_run` does not supply independent feed/clock evidence, and `live-check` supplies false/unknown evidence. The existing go/no-go check also reads and then writes a go/no-go record; redesign provenance and lifecycle so a previous failure cannot be overwritten or manufactured into a pass.
- [ ] Implement independently sourced, fresh, timestamped feed and absolute clock-drift checks, trusted operator approval and expiry, capital/risk validation, current kill level, remote alert proof and full reconciliation. Verify the evidence remains valid immediately before any order. Unknown, stale, future, malformed, expired, inconsistent or inaccessible evidence must fail closed.
- [ ] Build an independent venue snapshot that covers **total** balances, all open orders and actual protective order status/quantity/side, including orders created outside this process. Current CCXT adapter methods report free balances and/or process-local orders and are not adequate proof. Compare against ledger and exchange-confirmed fills; handle partial fills, unknown orders, API errors, rate limits, disconnected/stale snapshots and venue-specific semantics.
- [ ] Require verification before restart, resumption, cancellation or flattening. A submitted stop is not proof of full protection; a submitted close is not proof of flatness. Prevent double selling and oversize exits, especially for venues without reliable reduce-only semantics. No new exposure while state is uncertain.
- [ ] Test loss of the watchdog, old process still alive, races during escalation, fresh changed heartbeat, bounded restart budgets, reboot, repeated failures, crash during reconcile, partial fills and unprotected positions. Keep live/testnet auto-restart blocked until the safety properties are proven with realistic exchange/testnet evidence. Do not mark the Python supervisor as reboot-resilient without an independently tested host supervisor.

### 8C. Complete heal playbooks and post-heal checks

- [ ] Implement each outstanding row listed in `docs/BUILD_PLAN_20_21.md`: explicit five-failures-in-ten-minutes fallback; candle repair and marked affected windows; safe database lock/corruption recovery; controlled memory restart; clock-drift handling; model rollback and feature quarantine; valid last-known-good config; venue status/maintenance handling; failed-deployment rollback; and any other rows still partial.
- [ ] For each row implement detect → bounded action → independent verification → audited result, retry/cooldown and escalation. A check that merely reruns itself is not a repair. No healer may increase risk. Define what cannot be auto-repaired and escalate to a human.
- [ ] Implement the complete post-heal checklist before returning to L0: fresh feed and clock, safe venue orders/protection, clean reconciliation, stable health for the required interval, kill/hold state, alert availability and audit. Test every failure and ensure it blocks resume.

## Phase 9 — implement self-sustaining components (E–H)

### 9A. Market graph (E)

- [ ] Design the DuckDB schema for timestamped asset/strategy/source nodes and relationships. Include input provenance, as-of times, versioning and expiry; prevent future-data leakage.
- [ ] Calculate correlation, lead–lag and beta using declared windows and sufficient data; handle missing, stale and inconsistent data. Apply contagion/exposure cuts conservatively; no automatic exposure increase as a response to uncertain data.
- [ ] Test reproducibility, window boundaries, missing data, false positives and cuts through actual risk/order paths. Record comparisons with recorded historical data.

### 9B. Bounded allocator (F)

- [ ] Implement inverse-volatility allocation with shrunken performance tilt, a 35% maximum per strategy, at most 10 percentage points of change per week and at least 20% cash. Rebalance no more than weekly and never at L1 or above. Respect existing hard risk/exposure limits.
- [ ] Use complete after-cost backtests against equal weighting, with separated training, OOS and holdout data. Ship a non-equal allocator only if it genuinely beats equal weighting after costs under the declared gate; otherwise remain on the safe baseline. Test zero/negative returns, stale metrics, new strategies, caps, time boundaries and concurrent allocation requests.

### 9C. Lifecycle (G)

- [ ] Implement the nine-state lifecycle defined by the project specification, including IDEA and RETIRED; document every state, permitted transition, authority, evidence and audit event rather than inventing unsupported transitions. Require the existing evaluation gates for promotion; add decay disable and the specified 60-day retirement policy.
- [ ] Test prohibited transitions, insufficient data, time expiry, failure/restart, drift, disabling while positions/orders exist, and human review. Healers/models must never re-enable a disabled strategy.

### 9D. Fund loop, scheduling and operator absence (H)

- [ ] Build a reliable cost ledger, self-funding ratio, break-even calculations, profit waterfall, 30% tax reserve and budget caps, using actual costs and conservative unknown values. A reserve is not tax advice or proof of actual tax liability; consult a qualified Indian tax professional.
- [ ] Add continuous drift checks, weekly retraining, monthly research and quarterly audit scheduling with durable state and restart-safe idempotency. Research only recommends; it cannot promote itself. Meta-labels only remove trades; sentiment can only shrink size within [0.5, 1.0]. Learned cards only veto, expire after 14 days, and every attempt is logged.
- [ ] Implement authenticated operator-presence evidence and the 7-day warning, 14-day half-size and 21-day L1 absence ladder. Use durable timestamps, explicit timezone handling, monotonic safety behavior, restart recovery and fail-closed handling for missing/corrupt/future timestamps. Test thresholds and attempts to spoof presence.

## Phase 10 — dashboard, game day, packaging and release (I)

1. [ ] Complete dashboard Health, Autonomy, approval and go/no-go views using actual durable data and authorization boundaries. Distinguish missing data, optional degraded components and overall state. Preserve read-only defaults, protect reads with a real deployment access boundary, escape untrusted strings and avoid exposing credentials.
2. [ ] Implement uptime, per-row MTTR, healing/escalation and flapping reporting and a monthly paper game day. Establish reproducible fault scenarios and targets (feed <10 s, worker <5 s, restart plus reconciliation <60 s); measure rather than claiming them. Stop if a scenario cannot verify its repair.
3. [ ] Complete tests for every playbook row and negative scenario; run tests with real dependencies, plus repeatable integration/fault tests against a safe test environment. Run full local checks, build a wheel and verify the dashboard template is included. Verify CI on the combined PR. Run Docker Compose only after reviewing configuration, secrets, mounted data and network exposure; a dashboard `/healthz` response only proves HTTP liveness, not trading safety. Arena did not run Docker.
4. [ ] Conduct a code/security review independent of the implementer, review config and migrations, rehearse rollback and restore. Keep all D–I work in one combined PR as requested; merge only when complete and CI/reviews pass. Do not commit credentials, downloaded archives, databases, backups or generated models.

# Part C — external proof and controlled sign-off

## Phase 11 — testnet only after implementation and review

1. [ ] Select a legitimate exchange/testnet and read its current API and sandbox documentation. Confirm its behavior, symbols and data source. Create **separate testnet keys**, with no live account permissions and no withdrawals. Do not assume a testnet proves production order semantics.
2. [ ] Review the completed version's testnet settings and safety controls with the independent reviewer. Currently the testnet adapter is not certified and watchdog restart is blocked; do not run it unattended or against any funded account. There is no current `live-check` command that certifies testnet.
3. [ ] Once the missing code is complete, rehearse entry, rejection, cancellation, partial fill, protective stop, trigger, manual intervention, restart, external order/balance mismatch and disconnect. Compare recorded internal state with exchange-confirmed totals, open orders and fills after every case. Confirm the system never adds risk during uncertainty.
4. [ ] Run the planned two-week monitored testnet soak only after those cases pass, with logs, independent alert delivery, incident handling, tested rollback and backups. Verify all observed discrepancies and repeat failed scenarios. Never treat testnet success as a profitability result.

## Phase 12 — final independent go/no-go; live remains blocked today

1. [ ] Complete all software work and evidence above, have an independent reviewer verify source and tests, and confirm the exact reviewed release and clean config hashes. Obtain independent evidence for feed freshness, clock drift, reconciliation, venue controls, safety actions, approval identity, remote alerts and audit. Current `live-check` and live start are deliberately unable to pass these checks; **do not change them to return true or manually write a passing record**.
2. [ ] Review and sign every item in `docs/GO_NO_GO.md`, correcting any outdated assertions in that file first. Verify OOS/holdout provenance, all thresholds, actual cost-stress evidence, slippage, regime and multiple-testing analysis, 30-day paper soak, meaningful parity, fault tests, testnet evidence and restoration drills. Missing evidence or any failed gate means NO GO.
3. [ ] Decide personally whether you can afford a total loss. Confirm exchange/legal availability in your jurisdiction and seek qualified tax/legal advice. Secure the account with 2FA and offline recovery, dedicated restricted subaccount, a trade-only API key with withdrawals disabled, IP restrictions where supported, and an independently verified small capital limit. Keep live keys out of code, Git, logs and chat.
4. [ ] Prepare an always-on, monitored deployment with tested TLS/access controls, independent alerts, watchdog, reboot recovery, backup/restore, incident contacts and a manual exchange-side emergency procedure. Review both deployment and rollback. A machine that sleeps, untested recovery, missing remote alerts, or unknown venue state is a NO GO.
5. [ ] Only after the reviewed future implementation can genuinely pass a fresh, authenticated go/no-go and all human reviews agree, follow that reviewed release's runbook for an explicitly supervised, small-capital launch and staged ramp. Never assume the current code can do this; no live-start command is supplied here. Any critical incident, reconciliation discrepancy, unprotected position or failed safety gate requires stopping new risk, checking the venue directly and returning to paper pending review.

## Evidence worksheet

Keep a private entry for each phase:

```text
UTC date / operator / independent reviewer:
Commit hash and branch / local changes:
Config and risk versions (no secrets):
Data source, exact UTC range, symbols, gaps and hash:
Command or test / expected behavior / actual output and exit code:
Trades, fees, drawdown, OOS and holdout evidence where relevant:
Fault injected / detection / repair / independent verification / timing:
Exchange/testnet evidence and alert delivery (redacted):
Backup and restore test:
PASS / FAIL / BLOCKED, reason, next action:
```

A completed worksheet requires actual evidence, not only checked boxes. Update the project checklist and keep this procedure aligned with the code as the implementation evolves.

> **Promotion preflight:** the live go/no-go gate intentionally fails while evidence provenance
> and the actual 2× cost rerun are not integrated. Re-running `live-check` or editing a local
> `go_nogo` row cannot authorize live trading. The reconciliation requirement cannot be disabled
> by configuration. The latest guard result is stored separately as `live_guard_latest`.

### Read-only venue observation (diagnostic, not reset evidence)

After you have independently configured a testnet account and its credentials locally,
`venue-observe` queries CCXT's balance endpoint and its no-symbol open-orders endpoint:

```powershell
tradebot venue-observe --mode testnet
```

This prints sensitive account balances and order identifiers to your terminal. Protect any
copy or redirected file; do not paste credentials or account output into chat. The command
places and cancels no orders. It requires both endpoints to be reported as supported and
fails on malformed responses or timeouts. The requests are separate, not an atomic snapshot;
results may be paginated or incomplete and no venue behavior has been verified. A successful
command explicitly reports `orders_complete: false` and `usable_for_reset: false`. It does
not compare against the local ledger, clear a hold, reset the kill switch, authorize trading,
or replace an exchange-app review. `--mode live` would query the actual live account; do not
run it against real money as a substitute for testnet validation and the remaining gates.

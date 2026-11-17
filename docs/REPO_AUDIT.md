# Repository-wide recheck — 2026-11-04

This is an audit of the current working branch, not a certification of the trading
system. See `CHECKLIST.md` and `BUILD_PLAN_20_21.md` for remaining scope.

## Checks performed

- Inspected source packages, CLI entry points, configuration, dashboard, risk and
  safety paths, CI, packaging, Docker configuration, and project documentation.
- Full suite: **758 passed, 2 skipped**. The skipped tests were a synthetic run that
  ended flat and a sample that did not meet a test's rejection condition. This is
  not evidence of live execution or profitable trading.
- Ruff, mypy (88 source files), `git diff --check`, dependency consistency,
  configuration validation and `detect-secrets-hook` passed locally.
- Built a non-editable wheel and verified the dashboard HTML is included. Compose
  YAML and healthcheck placement were checked; Docker was not available to run.
- Paper/synthetic dashboard is hosted read-only on port 8000 at 1×. Local HTTP
  works; Arena browser preview routing has not been verified. Remote alerting is
  degraded without a configured and tested remote transport.

## Defects repaired in this audit

- The built wheel omitted the dashboard HTML. It is now packaged and CI checks it.
- Dashboard controls were enabled by default even without a password. Controls
  now require explicit opt-in and a password, with entries paused before flatten.
  Read-only APIs are **not authenticated**: protect the entire deployment separately.
- Dashboard risk panel used daily P&L as drawdown. It now computes drawdown from
  persisted peak equity. Untrusted displayed strings are escaped.
- Separate processes could overwrite higher kill levels with stale decisions, and
  a two-second cache could hide an external escalation. Escalations, initialization,
  and guarded L1 resumes now check persisted state transactionally; the cache is gone.
  This does not enforce all forbidden mutations system-wide.
- Promotion results could omit 2× cost stress and report a pass without evidence.
  The metric verdict now fails for missing cost stress or OOS provenance and uses
  the specified strict thresholds. OOS provenance is not yet verified by this
  pipeline, so this verdict deliberately cannot promote a strategy today.
- The Docker image's HTTP healthcheck incorrectly applied to bot and watchdog
  containers that do not serve HTTP. It is now restricted to the dashboard service;
  that check only establishes HTTP liveness, not trading health.
- CI's secrets scan previously suppressed failures. It now runs the baseline hook
  without masking its exit status.

## Important remaining limitations

Authenticated, action-bound approvals and full safety mutation boundaries are not
built. Market graph, allocator, lifecycle, fund loop, operator-absence ladder,
complete healing playbooks and game-day/MTTR evidence remain outstanding. The
live-check and live start fail closed because independent feed and clock evidence
is not wired. Exchange, Telegram, remote alerting and Docker integrations were not
verified against real services here. The synthetic parity smoke test observed no
trades or fills and is not execution-parity evidence. Existing synthetic backtests
do not establish profitability or promotion readiness.

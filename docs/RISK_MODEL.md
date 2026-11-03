# Risk model

Every number is in `config/risk.yaml` (limits) and `config/settings.yaml` (portfolio).
Changing a limit is a code review event, not a config edit at 2 a.m.: the risk engine
also enforces a **hard envelope** (`RiskLimits.ABSOLUTE_MAX_*`) that no YAML file can
exceed.

## 1. Pre-trade checks (all seventeen, in this order)

| # | Check | Default | Why |
| --- | --- | --- | --- |
| 1 | Kill switch at L0 | L0 | nothing opens while paused |
| 2 | Mode permits entries + ramp multiplier | paper/testnet/live guards | live starts at 10% size |
| 3 | Data fresh for the symbol | ≤ 15 s | never trade a stale book |
| 4 | Symbol enabled, strategy not paused | config + pauses | decay and loss streaks pause strategies |
| 5 | Spread | ≤ 0.15% | illiquid moments are not tradeable |
| 6 | Fat-finger | price within 1% of mid | catches a bug that sends a wild price |
| 7 | Notional window | 10–150 USDT | below min notional is the venue's reject; above max caps damage |
| 8 | Risk per trade | ≤ 0.5% of equity at the stop | size = risk budget ÷ stop distance |
| 9 | Per-symbol exposure | ≤ 20% of equity | one coin cannot dominate |
| 10 | Total exposure | ≤ 60% of equity | keeps cash for stops and shocks |
| 11 | Correlated bucket (BTC/ETH/SOL) | ≤ 40% of equity | crypto moves as one asset |
| 12 | Open positions | ≤ 5 | caps simultaneous damage |
| 13 | Order size vs top-10 book depth | ≤ 10% | you cannot be the whole book |
| 14 | Daily / weekly loss limits | −2% / −5% | stops the day before it becomes a week |
| 15 | Cooldown after a stop-out | 15 min | no revenge re-entry |
| 16 | Order rate | 10/min, 300/day | protects the API key and the operator |
| 17 | Cost budget: target distance vs round-trip cost | target ≥ 4× (2 taker fees + calibrated slippage) | a target barely above cost has no room to be right before the stop is hit |

Rejections are logged with the first failing check and stored in `risk_verdicts`.
Sizing starts from risk and then applies caps 9–13; the **smallest wins** and the
binding constraint is recorded, so "why was this trade so small" always has an answer.

The plan's sanity check holds: five positions at 0.5% risk each is ≈ 2.5% if they all
stop out together — above the −2% "stop new entries" line, below the −3% flatten line.

### The cost budget, and what it is not

Check 17 refuses an entry whose *target* is nearer than `min_target_cost_ratio` times the
round-trip cost, where the round-trip cost is two taker fees (always known, from the
venue's market rules) plus expected slippage (only once the slippage model has been fitted
on at least 30 fills; before that the fee half is still counted and the detail line says
the estimate is partial; if the rules themselves are unknown the check reports "cost
estimate unavailable" and passes, because a made-up number is worse than an admitted gap).

It is not a profitability test, and it is not a substitute for the gates. Measured on ten
synthetic days (`docs/RUNBOOK.md` has the command):

| ratio | trades | fees | equity | PF | what it means |
| --- | --- | --- | --- | --- | --- |
| 0 (off) | 97 | 27.69 | 971.23 | 0.91 | the unfiltered baseline |
| 4 (default) | 97 | 27.69 | 971.23 | 0.91 | every target in this run already clears 4× cost, so it is neutral here (on the 4-day robustness window it vetoes 1 trade of 27) |
| 8 | 69 | 19.47 | 993.70 | 1.03 | prunes 29% of trades, still a loss |
| 16 | 12 | 3.28 | 1028.95 | 5.54 | PF and Sharpe pass, trade count fails: a small sample, not an edge |

The 16 row is the one to remember. Raising the ratio until the numbers look good produces
twelve trades and a Sharpe of 6.37 that the acceptance gates refuse for the only honest
reason: twelve trades is not evidence. The default is 4 because it is neutral on this data
and would still refuse a genuinely unreachable target on a wider cost venue - not because
it made a backtest look better.

## 2. Loss handling

| Event | Action |
| --- | --- |
| Stop hit | exit at market; stop is never moved away from price |
| Fill worse than 2× expected slippage | WARN + cost assumption update |
| 4 consecutive losses in a strategy | size × 0.5 for that strategy |
| 6 consecutive losses | strategy paused 24 h |
| Day −2% | no new entries; existing positions keep their stops (L1). Lifts at the next trading day, when the day baseline is re-based |
| Day −3% | flatten everything (L2) |
| Week −5% | everything paused until reviewed (L1, manual reset) |
| −10% from peak equity | lockdown (L3): flatten + 1 h cooldown + written reason |
| Trade that goes nowhere | time stop after `time_stop_bars` |

## 3. Profit handling

* 50% out at **1.5R**, stop to break-even.
* Remaining position trails with a **3-ATR chandelier** stop (only upwards).
* Day +3% → size halved; day +5% → no new entries (both configurable; backtest
  whether they help before trusting them).
* Compounding is capped at **1.5× starting capital** so a hot streak cannot inflate risk.
* **Tax reserve**: 30% of net realized gains is tracked in a virtual ledger; the
  report shows post-tax PnL next to pre-tax. The bot never moves money.
* **TDS**: 1% on sales is tracked for records (India, verify current rules).

## 4. Kill switch

| Level | Meaning | How it is cleared |
| --- | --- | --- |
| L0 | normal | — |
| L1 PAUSE | no new entries, positions keep their stops | `/resume`, CLI `resume`, dashboard — or automatically, when the cause is scoped and gone (below) |
| L2 FLATTEN | cancel everything, close positions at market | manual reset after review |
| L3 LOCKDOWN | L2 + no restart | written reason + 1 h cooldown + manual reset |

**What may clear an L1 by itself.** Escalation is automatic; de-escalation is narrow and
recorded. Only these two paths exist, both written to `kill_events` and the event log:

| Cause | Clears when | Why it is safe |
| --- | --- | --- |
| `daily_loss` | the next trading day, after `day_start_equity` is re-based and the limit is verified satisfied | the limit is defined against day-start equity, so a new day makes it true again by construction |
| `stale_feeds`, `exchange_error_rate`, `reconciliation`, `disk_full` | after `execution.auto_resume_min_healthy_seconds` (default 600 s) of *verified* health | a transient fault, not a risk breach; the condition is re-checked, never assumed |

The weekly loss limit, a drawdown, a manual pause, a KILL file and everything at L2/L3 are
never cleared automatically — those are decisions a human has to make.

Automatic triggers, all persisted in SQLite so a restart cannot clear them:

1. daily loss limit hit (−2% L1, −3% L2)
2. drawdown limit (−10% L3)
3. weekly loss limit (−5% L1)
4. all feeds stale > 60 s (L1)
5. order error rate > 20% over 5 minutes (L1)
6. reconciliation mismatch (L1 + incident)
7. clock drift > 1 s (live preflight and health loop)
8. 5 order rejects in 5 minutes (L1)
9. strategy exception loop (strategy paused + WARN)
10. watchdog heartbeat lost > 10 s (L2, raised by the watchdog process)
11. disk above 95% (L1; 80% is a warning)
12. unknown position or order found during reconciliation (L1)

Manual routes: Telegram `/pause /flatten /kill`, dashboard buttons with confirmation,
CLI `pause|resume|kill|lockdown`, and a `KILL` file in the project root (checked every
health tick and by the watchdog).

The fire drill (`tradebot drill`) opens risk, kills the bot and measures time-to-flat;
the target is under 10 seconds in paper.

## 5. Paper exchange realism

Paper results are only worth something if paper is pessimistic:

* market orders **walk the live book** (size moves the price);
* fees 0.1% maker / 0.1% taker by default;
* latency 100–500 ms between decision and fill;
* limit orders fill only when price **trades through** the level, not merely touches it;
* min notional and lot-size rejects, plus a 0.1% random API failure rate;
* insufficient cash is a hard reject, never a negative balance.

## 6. Live-mode gates

All must pass, or the bot refuses to start live (`tradebot live-check` shows them):

1. `mode: live` **and** `LIVE_TRADING_ENABLED=true`;
2. typed confirmation containing the capital amount and the word `LIVE`;
3. a go/no-go record in the database, less than 14 days old;
4. API key with trading permission, **withdrawals disabled**, IP whitelist enforced —
   verified from the exchange, and *unverifiable counts as failed*;
5. a hard `max_capital_usdt` cap;
6. preflight: kill level L0, feeds fresh, clock synced, reconciliation clean, database
   writable, Telegram test delivered;
7. ramp: 10% → 25% → 50% → 100% size, each stage needing its minimum days with zero
   critical incidents (a stage also does not advance if live costs exceed paper
   assumptions by more than 50%).

Switching back to paper needs one setting and no confirmation. Paper, testnet and live
use separate database files and separate API keys.

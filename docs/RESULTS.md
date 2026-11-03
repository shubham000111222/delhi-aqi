# Results — measured, reproducible, and mostly negative

Everything below was run on **2026-11-03** from commit `8486021` (plus the dashboard veto
panel) in this repository, with the default config, on the **seeded synthetic market**.
Nothing here is a claim about real crypto markets: the synthetic feed has regime shifts and
correlated moves, which is enough to exercise the plumbing and nowhere near enough to
establish an edge.

Reproduce any row with the command in it. If a number here disagrees with a rerun, the
rerun is right and this file is stale — fix it rather than the other way round.

## Headline

| Run | Result | Verdict |
| --- | --- | --- |
| `backtest --days 10` | 97 trades, win 38.1%, fees 27.69, equity 971.23 (−2.9%) | **NOT PROMOTABLE**: PF 0.91 (need ≥ 1.3), Sharpe −9.77 (≥ 1.0), DD −7.63% (≤ 15%), 97 trades (< 200), MC p95 DD −8.02% (≤ 20%) |

Two of five gates pass. The three that fail are the three that matter, and the reason is
cost: 27.69 USDT of fees on a 1,000 USDT account over ten simulated days is 2.8% of equity.

## The validation suite

| Check | Command | Result |
| --- | --- | --- |
| Walk-forward, 3 out-of-sample windows | `walk-forward --train-days 3 --test-days 1 --windows 3 --seed 5` | Windows +0.43%, −2.04%, +0.21% → compound **−1.42%**, "inconsistent"; OOS PF 0.20, Sharpe −11.4, 19 trades → **NOT PROMOTABLE** |
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

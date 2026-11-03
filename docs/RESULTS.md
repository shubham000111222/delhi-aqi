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

## What is not measured here, and why

Live order placement, a WebSocket session, a real backfill, Telegram's API and FinBERT
weights all need network egress or credentials this sandbox does not have. Each is listed
with its next step in `ROADMAP.md` → "Not built". The live ccxt adapter's logic is covered
by 27 tests through a fake exchange, but no test here can prove what Binance will answer.

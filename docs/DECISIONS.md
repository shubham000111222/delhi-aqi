# Decisions and open questions

## Decisions taken while building this

| # | Decision | Reason | Consequence |
| --- | --- | --- | --- |
| 1 | **Synthetic feed is the default** | the build environment has no exchange connectivity; a deterministic seeded market makes every test and demo reproducible | `--feed ccxt` is implemented but unproven; swap it after one testnet run |
| 2 | **Single sequential event loop, no worker pool** | determinism and replayability beat throughput at this scale (5 symbols, 2 signal timeframes) | 15-minute strategy bars, 1-minute data; do not expect HFT |
| 3 | **Decimal everywhere for money, float only for indicator math** | float money bugs are unrecoverable; float indicators are fine | candle → float conversion at the strategy boundary |
| 4 | **SQLite for state, with `:memory:` for backtests** | one file per mode, WAL, survives restart, trivial backup | no horizontal scaling; fine for one bot |
| 5 | **Exits are market orders; entries are limit with a timeout** | getting out matters more than the last basis point | exit slippage is measured and reported |
| 6 | **Local stop monitoring in all modes, plus exchange-side stop in live** | the local monitor is the fallback if the exchange stop fails; the exchange stop protects you if the bot dies | the local monitor cancels the resting stop before exiting, so they cannot both fire |
| 7 | **Recovery adopts open positions instead of flattening on restart** | flattening on every restart would destroy a live position on a 5-second deploy | reconciliation must be clean before entries resume |
| 8 | **Rate/order windows use the injected clock** | replaying two days in two seconds must not look like a rate storm | `TokenBucket`/`SlidingWindowCounter` take `now_fn` |
| 9 | **Entry plans are keyed by client order id** | two limit entries on the same symbol-strategy must not share a plan, and a fill that arrives inside `place_order` must still find it | `OrderManager.plans` + `plan_for(order_id)` |
| 10 | **The dashboard can host the bot** | one command should give a working demo | `--no-bot` for read-only against a separately running bot |
| 11 | **The LLM journal is template-first** | a report must never depend on an API call, and numbers must be checkable | every number in LLM output is cross-checked against the day's JSON or the template wins |
| 12 | **Strategies S4/S5 are inert, not stubbed with fake signals** | a strategy that ships without validation teaches the wrong lesson | `MlSignal.evaluate` returns `None` until an approved model card exists |
| 13 | **Sensitivity judges the sign before the shape** | a wide, smooth plateau of *negative* scores was being reported as "broad plateau", which reads like approval | best cell ≤ 0 ⇒ "no edge", whatever the shape |
| 14 | **Parity needs a 1 200-minute tape, not 240** | short runs produced 0 decisions, so a "MATCH" proved nothing; the test now asserts decisions and fills are non-zero | parity is slow (minutes) and worth it |
| 15 | **IC is measured on non-overlapping samples** | forward returns over a horizon overlap bar to bar; counting the same move H times inflates the IC and shrinks its standard error to fiction | fewer observations, an honest noise band printed alongside every IC |
| 16 | **Fills are measured against the decision price** | the paper adapter used the fill price as the benchmark for market orders, so measured slippage was identically zero | `Order.reference_price`; slippage p95 is now +26 bps rather than 0 |
| 17 | **Cost stress reruns the backtest instead of subtracting fees** | a 2× cost world also changes which orders fill and which stops trigger; post-hoc subtraction flatters the result | `tradebot robustness` runs the engine twice |
| 18 | **No model card is committed, and `models/*.json` is gitignored** | a model trained on the synthetic generator would otherwise be able to trade after a `git pull` | `S4` reports `not_promoted` and returns nothing |
| 19 | **Promotion of a more complex model is enforced in code** | a rule kept in a document is a rule that gets ignored the first time the complex model looks good | `ladder.promote()` needs +10% net expectancy, profitable, in every window |
| 20 | **Venue abstractions before the venue** | §18.8 asks for the seams now so an NSE adapter is an adapter job, not a rewrite | `venues/` exists with no NSE broker adapter behind it |
| 21 | **Order-book features go NaN when the snapshot is stale** | carrying a five-minute-old imbalance forward produces confident, wrong numbers | staleness limit of five bars, tested |
| 22 | **The stream transport is injected, not imported** | no exchange access here, so a feed that could only be tested against a live socket would be untested code on a money path | `WebSocketFeed(config, clock, transport=...)`; the tests feed it recorded Binance frames |
| 23 | **Gap fill runs before the first frame after a reconnect, and windows end at that frame** | filling on connect used the wall clock, which on a replay never moves, so the window started where it ended and filled nothing - an outage that looks like continuity | the filled bars are emitted in order, ahead of the new frame; `stream` prints `gap-filled bars` |
| 24 | **Exchange symbols are rewritten to the configured ones at the boundary** | Binance streams `BTCUSDT`; the engine keys on `BTC/USDT`, so every bar would land under a symbol nothing looks up and the bot would sit flat while believing it was receiving data | `WebSocketFeed._canonicalise`, asserted in tests |
| 25 | **Meta-labelling may only remove trades** | a model that sizes positions is a second risk engine, unauthorized and unreviewed; §19.3 asks for a filter | the gate returns take/skip only; size stays with `portfolio/sizing.py` |
| 27 | **The Kite adapter uses `tag` as its client order id, with a lookup before every place** | Kite has no idempotent client id; a retry after a timeout would otherwise double a position | `_live_order_for_tag` runs first, and again after a transient failure |
| 28 | **NSE rules are checked locally before the order is sent** | a broker rejection for a 5-paise tick or a lot step has already been counted as an attempt, and pollutes the trade log | `Instrument.validate` raises `PermanentExchangeError`; nothing reaches the wire |
| 29 | **A closed NSE session is a permanent error, not a retry** | a MARKET order queued at 16:00 IST is a caller bug, and retrying it all evening is worse | `PermanentExchangeError("market closed")`, tested |
| 26 | **Sentiment can only shrink a position, never grow it** | a lexicon (or an LLM) reading headlines is easily wrong and easily manipulated | multiplier in [0.5, 1.0]; a broken feed leaves sizing untouched |

## Decisions I need from you (plan §17.4)

1. **Exchange for public data and (later) execution** — Binance, Bybit, CoinDCX, WazirX,
   Delta? Check availability and terms for India, and that its public REST endpoints
   are reachable from your machine/VPS. Until then the bot runs on the synthetic feed.
2. **Starting virtual capital** — default is 1,000 USDT; the hard capital cap for live
   is 250 USDT in `settings.yaml`.
3. **Coin list** — default BTC, ETH, SOL, BNB, XRP. Fewer coins means fewer fees and
   less correlation risk.
4. **Hosting** — your PC for paper (disable sleep, wired network) or a small VPS before
   live (static IP for the API whitelist). Docker Compose is included for either.
5. **LLM provider and Telegram bot token** — optional. Reports and alerts fall back to
   disk and logs without them.
6. **Quote currency** — USDT vs USDC vs INR pairs changes fees, tax records and depeg
   exposure. Record your choice here: `______________`.

## Next three moves (in order)

1. Get **real 1-minute history** for the five coins (backfill into the Parquet archive),
   then re-run `backtest`, `walk-forward`, `sensitivity`, `robustness` and `signals` on
   real data. Expect most strategies to be rejected; the suite is built to say so.
2. Start the **30-day paper soak** on live prices (`--feed ccxt`), recording books to the
   archive so the slippage model is calibrated on a real venue instead of the simulator.
3. Only then consider testnet, keys, and the live ramp - and only for a strategy that
   cleared every gate, including the 2× cost rerun.

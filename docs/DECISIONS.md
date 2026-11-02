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
| 12 | **Strategies S4/S5 are inert, not stubbed with fake signals** | a strategy that ships without validation teaches the wrong lesson | `MlSignal.evaluate` returns `None` and says so |

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

1. Get **real 1-minute history** for the five coins (backfill), then re-run the
   backtest and the gates on real data. Expect some strategies to be rejected.
2. Add the **walk-forward runner and parity test** so a strategy can be validated
   properly, and start the 30-day paper soak on live prices (`--feed ccxt`).
3. Only then consider testnet, keys, and the live ramp.

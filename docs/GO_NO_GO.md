# Go/no-go checklist before real money

Copy this into an issue and tick every box. The bot refuses to start in live mode
unless the go/no-go record it writes to the database is **passed** and **less than 14
days old** (`tradebot live-check` regenerates it).

## Validation

- [ ] Strategies S1–S3 backtested on real exchange history (≥ 2 years, not synthetic)
- [ ] Out-of-sample profit factor > 1.3 after costs
- [ ] Sharpe > 1.0 and max drawdown < 15%
- [ ] At least 200 trades in the out-of-sample window
- [ ] Still profitable when the **backtest is rerun** at 2× fees (`tradebot robustness`), not merely after subtracting the extra fees
- [ ] Monte Carlo 95th percentile drawdown < 20%
- [ ] Results split by regime: bear (2022), range, bull — none carried by one period
- [ ] Multiple-testing: the best Sharpe beats the maximum expected from luck alone over the number of trials in `experiments` (`tradebot sensitivity` prints both)
- [ ] Holdout window touched exactly once, and the run is flagged in the database
- [ ] Parity: one recorded live day replayed through the engine matches decision-for-decision (`tradebot parity --minutes 1200` prints `MATCH`; a short run with zero decisions does not count)
- [ ] Sensitivity: the parameter plateau is broad *and* its best cell is profitable
- [ ] Signal screen: any model used reports its IC noise band, and no signal is treated as proven when its IC sits below that band
- [ ] Leakage checks pass: shuffled labels collapse, far-shift IC is inside the noise band, and no single feature carries > 40% of the model's weight
- [ ] Slippage model calibrated on real recorded books, and its calibration is not optimistic for more than 60% of fills
- [ ] The live entry cap (`execution.max_slippage_bps`) is fitted on real fills, not synthetic ones, and the
      dashboard's execution panel shows p95 inside the budget
- [ ] The research loop's recommendation (if any) has been re-tested by walk-forward and robustness -
      a search result is a hypothesis, never a promotion
- [ ] Sentiment, if enabled, is fed by a feed you trust, and its size multiplier has never exceeded 1.0 in
      a day's event log (`event = sentiment_sizing`)

## Paper soak

- [ ] 30 consecutive days of paper trading on live prices
- [ ] Zero unhandled exceptions, zero reconciliation mismatches
- [ ] Kill-switch fire drill completed in the last 30 days (time-to-flat < 10 s)
- [ ] Deployment and rollback rehearsed once in paper mode
- [ ] Daily reports reviewed daily; a missed heartbeat alert noticed at least once

## Infrastructure

- [ ] VPS or always-on machine, static IP, sleep disabled, wired network
- [ ] Docker Compose with `restart: unless-stopped` for bot, watchdog, dashboard
- [ ] Daily database backups, 30-day retention, restore tested this month
- [ ] Disk < 80%, memory and CPU headroom measured during a soak
- [ ] NTP synced, drift under 1 s
- [ ] Logs rotating, no secrets in them (gitleaks clean)

## Exchange and keys

- [ ] Dedicated sub-account or small deposit; `max_capital_usdt` set to money you can lose entirely
- [ ] API key: trade-only, **withdrawals disabled**, IP whitelist to your static IP
- [ ] Testnet keys separate from live keys; testnet run completed first
- [ ] 2FA backup codes offline, not on the trading PC
- [ ] Exchange status page bookmarked; monitoring of announcements decided
- [ ] Quote currency decided deliberately (USDT vs USDC vs INR pairs) and recorded

## Counterparty and monitoring

- [ ] Depeg monitor active (USDC/USDT cross; WARN at 0.5%, L1 at 1%)
- [ ] Exchange status/announcements monitor active, entries paused before maintenance
- [ ] Telegram bot configured with a whitelisted chat id; destructive commands confirmed
- [ ] LLM journal budget capped, template fallback verified
- [ ] Emergency runbook printed and on your phone

## Behaviour rules (read and accept)

- [ ] No plan guarantees profit; success is correctness and safety
- [ ] I will not loosen risk limits after a loss (the engine enforces a 24 h delay on loosening)
- [ ] Any critical incident, or two consecutive daily-loss-limit days, sends the bot back to paper
- [ ] Ramp stages advance only with the minimum days **and** zero critical incidents
- [ ] I understand a long-only spot system sits in cash through sustained downtrends

## Sign-off

```
date (UTC):            ____________
capital committed:     ____________ USDT
max_capital_usdt:      ____________ USDT
ramp stage:            0 (10% size)
reviewed by:           ____________
```

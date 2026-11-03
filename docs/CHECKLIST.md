# Completion checklist

The build plan's code is written; what remains is **evidence and access**. This file is the
one place that says what is left, in order, with the exact command, the pass criterion, and
what to send back. Work down it top to bottom; each phase produces a number the next phase
depends on.

*Status legend:* ✅ done · ▶ next · ⬜ not started · 🔒 blocked on something only you can do.

> **What "complete" means here.** Every component built, every external integration
> verified against the real thing, and an honest verdict on real recorded data. It does
> **not** mean "profitable": the acceptance gates are built to say no, and on synthetic
> data they already do. A project that reports its own failure is finished; one that
> manufactures a pass is not.

**Last updated:** 2026-11-04 · **Suite:** 607 passed, 2 skipped · **Archive as of this
note:** 30 days × 5 symbols of real 1m Binance candles (216,198 bars).

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

  **Pass:** 5 symbols, ~215 days, roughly 1.1M candles, and one `complete` line per chunk
  in `history.json`.

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
- [ ] **2.5** `python -m tradebot sensitivity --feed archive --strategy S3_breakout --param lookback=10,20,30,40 --days 120`

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
| 🔒 | Telegram | put `token` + `chat_id` in `config/settings.yaml`, then `python -m tradebot telegram` | prints reachable, message arrives |
| 🔒 | News overlay (optional) | `python -m tradebot news --url <rss>` | multiplier ≤ 1.0, never grows a position |
| 🔒 | MLflow (optional) | `mlflow server --port 5000` then `python -m tradebot experiments --read-server http://127.0.0.1:5000` | runs visible |
| 🔒 | NSE / Kite (optional) | `python -m tradebot venues --venue nse --notional 100000` with keys | session + cost model printed |
| 🔒 | Docker (optional) | `docker compose up` | dashboard on :8000 |

---

## Phase 5 — testnet, then the live ramp

Only after Phase 2 and Phase 3 pass. Live mode is refused unless every guard in
`risk/live_guards.py` holds, and the capital cap stays at 250 USDT.

- [ ] **5.1** `mode: testnet` + testnet keys → `python -m tradebot live-check --confirm` → every gate PASS.
- [ ] **5.2** Two weeks on testnet; re-run `slippage`, re-train the ledger card on testnet trades.
- [ ] **5.3** `mode: live` → `python -m tradebot live-check --confirm --with-adapter`.
- [ ] **5.4** First live week supervised: `status` twice a day, `drill` once, ramp enforced.
- [ ] **5.5** Promote only while `metrics` keeps passing; any failed gate drops back to paper.

---

## Phase 6 — housekeeping (monthly)

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

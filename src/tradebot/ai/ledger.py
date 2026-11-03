"""Learning from the bot's own trading: the closed-trade ledger as a training set (§19.3).

Every other model in this package learns from **bars**. This one learns from **fills** -
the strategy, symbol and volatility that were in force when a trade was opened, and the
result once it closed. It answers a narrower question than the meta-labeller ("did this
combination pay, net of costs?") and it is allowed to answer "I cannot know yet".

Four things make it usable rather than self-deceiving:

* **Features are decision-time only.** A trade contributes the volatility, the stop
  distance, the reward distance and the hour *as they were when the entry was decided*.
  Anything known only at exit (`bars_held`, `highest_price`, whether the target was hit)
  is outcome, not feature, and including it would produce a model that scores perfectly
  in sample and cannot be deployed.
* **Labels are net of fees, and re-stressed at 2x.** `net_r` is the realised P&L divided
  by the risk that was on the table, so a 1R win and a 1R loss are comparable across a
  $0.70 XRP position and a $45,000 BTC one. `stress_r` charges the fees a second time and
  is used in the promotion decision, because this repository's strategies die of fees
  before they die of anything else.
* **Walk-forward over the ledger, with a purge.** A gate trained on a trade that had not
  closed when the test window opened has already seen the answer. Training rows are
  restricted to trades that closed before the test window begins.
* **It takes 200 trades to earn a vote.** That is the same sample floor the acceptance
  gates ask for, and it is why the honest answer for a fresh paper account is "not yet"
  rather than a model. A gate fitted to 26 trades is a mood.

The promotion rule mirrors §19.2: the gate must beat taking every trade, on net R, in
**every** window, by at least 10%, with AUC above 0.55 and the 2x-cost rerun still
positive. Anything less and the card is not written, `learn` prints why, and the bot keeps
trading on the rule alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..core.clock import utc, utcnow
from ..core.logging import get_logger
from .meta import LogisticGate, auc_score

log = get_logger(__name__)

#: The sample floor. Below this a gate is a mood, not a model.
MIN_TRADES = 200
#: Fewer training rows than this in a window and the fit is noise.
MIN_TRAIN = 40
#: Fewer test rows than this and a window cannot say anything.
MIN_TEST = 20
#: Net-expectancy improvement the gate must show in every window (plan §19.2).
MIN_IMPROVEMENT = 0.10
#: AUC a gate must clear before it may veto anything.
MIN_AUC = 0.55
#: Cumulative-breakdown buckets for the hour of day.
HOUR_BUCKETS = 4

VERDICT_NEED_DATA = "NEED_MORE_DATA"
VERDICT_NO_EDGE = "NO_EDGE"
VERDICT_PROMOTABLE = "PROMOTABLE"


def _f(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return utc(value)
    if isinstance(value, str) and value:
        try:
            return utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def hour_bucket(moment: datetime) -> str:
    """A coarse time-of-day bucket. Four is deliberate: more would just fit noise."""
    return f"h{int(moment.hour) * HOUR_BUCKETS // 24}"


@dataclass(frozen=True, slots=True)
class LedgerTrade:
    """One closed trade, reduced to what was knowable at entry plus what it did."""

    strategy: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    net_pnl: float
    risk_amount: float
    fees: float
    net_r: float
    stress_r: float
    features: dict[str, float]

    @property
    def won(self) -> bool:
        return self.net_r > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "symbol": self.symbol,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "net_r": round(self.net_r, 4),
            "stress_r": round(self.stress_r, 4),
            "features": {key: round(value, 4) for key, value in self.features.items()},
        }


@dataclass
class LedgerReport:
    """The parsed ledger, plus what had to be thrown away and why."""

    trades: list[LedgerTrade] = field(default_factory=list)
    skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def _skip(self, reason: str) -> None:
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.won)

    def summary(self) -> dict[str, Any]:
        trades = self.trades
        if not trades:
            return {"trades": 0, "skipped": self.skipped, "reasons": self.skip_reasons}
        r = np.array([trade.net_r for trade in trades])
        return {
            "trades": len(trades),
            "skipped": self.skipped,
            "reasons": self.skip_reasons,
            "first": trades[0].opened_at.isoformat(),
            "last": trades[-1].closed_at.isoformat(),
            "win_rate": round(self.wins / len(trades), 3),
            "mean_r": round(float(r.mean()), 4),
            "median_r": round(float(np.median(r)), 4),
            "total_r": round(float(r.sum()), 2),
            "fee_share_of_gross": round(_fee_share(trades), 3),
        }


def _fee_share(trades: list[LedgerTrade]) -> float:
    """What share of gross P&L the fees ate. 1.0 means fees ate everything."""
    fees = sum(trade.fees for trade in trades)
    gross = sum(trade.net_pnl + trade.fees for trade in trades)
    if abs(gross) < 1e-9:
        return 0.0
    return max(0.0, min(9.99, fees / abs(gross)))


def trades_from_store(store: Any, *, since: datetime | None = None, limit: int = 100_000) -> LedgerReport:
    """Read closed positions and turn them into training rows.

    A row needs a risk amount (it is the denominator of every label) and an entry-time
    volatility and stop distance. Positions that predate those fields, or that were
    closed without a fill, are counted and skipped rather than imputed.
    """
    report = LedgerReport()
    for position in store.closed_positions(since=since, limit=limit):
        strategy = str(position.get("strategy") or "")
        symbol = str(position.get("symbol") or "")
        opened = _when(position.get("opened_at"))
        closed = _when(position.get("closed_at"))
        if not opened or not closed or not strategy or not symbol:
            report._skip("missing timestamps or identity")
            continue
        entry = _f(position.get("entry") or position.get("avg_entry"))
        stop = _f(position.get("initial_stop") or position.get("stop"))
        target = _f(position.get("target"))
        atr = _f(position.get("atr_at_entry"))
        qty = _f(position.get("initial_qty") or position.get("qty"))
        risk_per_unit = _f(position.get("risk_per_unit"))
        net_pnl = _f(position.get("realized_pnl") or position.get("pnl")) or 0.0
        fees = _f(position.get("fees")) or 0.0
        if entry is None or entry <= 0 or qty is None or qty <= 0:
            report._skip("no entry price or quantity")
            continue
        risk_amount = (qty * risk_per_unit) if risk_per_unit and risk_per_unit > 0 else None
        if risk_amount is None and stop is not None:
            risk_amount = abs(entry - stop) * qty
        if not risk_amount or risk_amount <= 0:
            report._skip("no risk amount (no stop)")
            continue
        if net_pnl > 0 and gross_of(net_pnl, fees) > risk_amount * 50:
            report._skip("implausible P&L for the risk taken")
            continue
        stop_bps = (abs(entry - stop) / entry * 10_000) if stop is not None else 0.0
        if stop_bps <= 0:
            report._skip("no stop distance")
            continue
        reward_bps = (abs(target - entry) / entry * 10_000) if target is not None else 0.0
        atr_bps = (atr / entry * 10_000) if atr else 0.0
        report.trades.append(
            LedgerTrade(
                strategy=strategy,
                symbol=symbol,
                opened_at=opened,
                closed_at=closed,
                net_pnl=net_pnl,
                risk_amount=risk_amount,
                fees=fees,
                net_r=net_pnl / risk_amount,
                stress_r=(net_pnl - fees) / risk_amount,
                features={
                    "atr_bps": atr_bps,
                    "stop_bps": stop_bps,
                    "reward_bps": reward_bps,
                    "rr": (reward_bps / stop_bps) if stop_bps else 0.0,
                },
            )
        )
    report.trades.sort(key=lambda trade: (trade.closed_at, trade.opened_at))
    return report


def gross_of(net_pnl: float, fees: float) -> float:
    return net_pnl + fees


# ---------------------------------------------------------------------------
# features
# ---------------------------------------------------------------------------


@dataclass
class Vocabulary:
    """The categorical values a card was trained on. Kept so a card cannot drift."""

    strategies: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    @classmethod
    def from_trades(cls, trades: list[LedgerTrade]) -> Vocabulary:
        return cls(
            strategies=sorted({trade.strategy for trade in trades}),
            symbols=sorted({trade.symbol for trade in trades}),
        )

    def names(self) -> list[str]:
        numeric = ["atr_bps", "stop_bps", "reward_bps", "rr"]
        return (
            numeric
            + [f"strat_{name}" for name in self.strategies]
            + [f"sym_{name.replace('/', '_')}" for name in self.symbols]
            + [f"hour_h{index}" for index in range(HOUR_BUCKETS)]
        )

    def vector(self, trade: LedgerTrade) -> list[float]:
        row = [trade.features.get(key, 0.0) for key in ("atr_bps", "stop_bps", "reward_bps", "rr")]
        row += [1.0 if name == trade.strategy else 0.0 for name in self.strategies]
        row += [
            1.0 if name == trade.symbol else 0.0 for name in self.symbols
        ]
        bucket = hour_bucket(trade.opened_at)
        row += [1.0 if bucket == f"h{index}" else 0.0 for index in range(HOUR_BUCKETS)]
        return row

    def as_dict(self) -> dict[str, Any]:
        return {"strategies": self.strategies, "symbols": self.symbols}


def design_matrix(trades: list[LedgerTrade], vocabulary: Vocabulary) -> np.ndarray:
    return np.asarray([vocabulary.vector(trade) for trade in trades], dtype=float)


# ---------------------------------------------------------------------------
# walk-forward over the ledger
# ---------------------------------------------------------------------------


@dataclass
class LedgerWindow:
    index: int
    start: datetime
    end: datetime
    n_train: int
    n_test: int
    auc: float
    base_r: float
    gated_r: float
    taken: int
    threshold: float
    stress_r: float

    @property
    def improvement(self) -> float:
        """Relative lift over taking every trade. A negative base flips the meaning."""
        if self.base_r == 0:
            return 0.0
        if self.base_r < 0:
            # going from -0.10R to -0.02R is a 0.08 improvement, not a -80% one
            return (self.gated_r - self.base_r) / abs(self.base_r)
        return (self.gated_r - self.base_r) / self.base_r

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.index,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "train": self.n_train,
            "test": self.n_test,
            "auc": round(self.auc, 3),
            "base_r": round(self.base_r, 4),
            "gated_r": round(self.gated_r, 4),
            "taken": self.taken,
            "threshold": round(self.threshold, 3),
            "stress_r": round(self.stress_r, 4),
            "improvement": round(self.improvement, 3),
        }


def _threshold(probabilities: np.ndarray, outcomes: np.ndarray) -> float:
    """The cut-off with the best net expectancy, not the best accuracy.

    Also requires the gate to keep at least a fifth of the trades: a threshold that keeps
    three trades can always look brilliant.
    """
    floor = max(5, int(len(probabilities) * 0.2))
    best, best_value = 0.5, -np.inf
    for threshold in (index / 20 for index in range(5, 20)):
        chosen = probabilities >= threshold
        if int(chosen.sum()) < floor:
            continue
        value = float(outcomes[chosen].mean())
        if value > best_value:
            best, best_value = threshold, value
    return best


def walk_forward(
    trades: list[LedgerTrade],
    *,
    windows: int = 3,
    vocabulary: Vocabulary | None = None,
) -> list[LedgerWindow]:
    """Purged walk-forward across the ledger.

    The test period is split into `windows` equal blocks. A window's training set is every
    trade that **closed before that block opened** - not every trade that opened before it,
    which would leak outcomes that were still unwinding.
    """
    if windows < 1 or len(trades) < (windows + 1) * 2:
        return []
    vocabulary = vocabulary or Vocabulary.from_trades(trades)
    ordered = sorted(trades, key=lambda trade: trade.opened_at)
    # the first 1/(windows+1) of the ledger is the initial training block; the rest is
    # cut into equal test blocks, so every window tests on later trades than it trained on
    first_test = len(ordered) // (windows + 1)
    block = max(1, (len(ordered) - first_test) // windows)
    results: list[LedgerWindow] = []
    for index in range(windows):
        start_index = first_test + index * block
        end_index = len(ordered) if index == windows - 1 else start_index + block
        test_rows = ordered[start_index:end_index]
        if not test_rows:
            continue
        start = test_rows[0].opened_at
        end = test_rows[-1].opened_at
        train_rows = [trade for trade in trades if trade.closed_at < start]
        if len(train_rows) < MIN_TRAIN or len(test_rows) < MIN_TEST:
            continue
        model = LogisticGate(alpha=1.0).fit(
            design_matrix(train_rows, vocabulary),
            np.asarray([1.0 if trade.won else 0.0 for trade in train_rows]),
        )
        if not model.ready:
            continue
        train_p = model.predict_proba(design_matrix(train_rows, vocabulary))
        train_r = np.asarray([trade.net_r for trade in train_rows])
        threshold = _threshold(train_p, train_r)
        test_p = model.predict_proba(design_matrix(test_rows, vocabulary))
        test_r = np.asarray([trade.net_r for trade in test_rows])
        test_s = np.asarray([trade.stress_r for trade in test_rows])
        chosen = test_p >= threshold
        if not chosen.any():
            continue
        results.append(
            LedgerWindow(
                index=index,
                start=start,
                end=end,
                n_train=len(train_rows),
                n_test=len(test_rows),
                auc=auc_score(test_p, np.asarray([1.0 if trade.won else 0.0 for trade in test_rows])),
                base_r=float(test_r.mean()),
                gated_r=float(test_r[chosen].mean()),
                taken=int(chosen.sum()),
                threshold=threshold,
                stress_r=float(test_s[chosen].mean()),
            )
        )
    return results


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


@dataclass
class LedgerVerdict:
    status: str
    reasons: list[str]
    windows: list[LedgerWindow] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def may_write_card(self) -> bool:
        return self.status == VERDICT_PROMOTABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "windows": [window.as_dict() for window in self.windows],
            "summary": self.summary,
        }

    def lines(self) -> list[str]:
        out = [f"verdict: {self.status}"]
        out += [f"  - {reason}" for reason in self.reasons]
        return out


def judge(
    report: LedgerReport,
    windows: list[LedgerWindow],
    *,
    min_trades: int = MIN_TRADES,
) -> LedgerVerdict:
    """Promote only on repeated, out-of-sample, cost-stressed evidence."""
    summary = report.summary()
    n = len(report.trades)
    if n < min_trades:
        return LedgerVerdict(
            status=VERDICT_NEED_DATA,
            reasons=[
                f"{n} closed trades, {min_trades} required before a model gets a say "
                f"(the same floor the acceptance gates use)",
                f"continue the paper soak: {min_trades - n} more trades",
            ],
            windows=windows,
            summary=summary,
        )
    if len(windows) < 3:
        return LedgerVerdict(
            status=VERDICT_NEED_DATA,
            reasons=[
                f"only {len(windows)} usable walk-forward window(s); 3 are required",
                "each window needs at least "
                f"{MIN_TRAIN} trades resolved before it and {MIN_TEST} inside it",
            ],
            windows=windows,
            summary=summary,
        )
    reasons: list[str] = []
    failing = [window for window in windows if window.improvement < MIN_IMPROVEMENT]
    if failing:
        reasons.append(
            "the gate did not beat taking every trade by 10% in "
            f"{len(failing)} of {len(windows)} windows (worst "
            f"{min(window.improvement for window in windows):+.0%})"
        )
    weak = [window for window in windows if window.auc < MIN_AUC]
    if weak:
        reasons.append(
            f"AUC below {MIN_AUC} in {len(weak)} of {len(windows)} windows "
            f"(min {min(window.auc for window in windows):.3f}): the ranking is close to noise"
        )
    if any(window.gated_r <= 0 for window in windows):
        reasons.append("the gated expectancy is not positive in every window")
    if any(window.stress_r <= 0 for window in windows):
        reasons.append("at 2x costs the gated trades stop paying - the edge is the fee")
    if reasons:
        return LedgerVerdict(
            status=VERDICT_NO_EDGE,
            reasons=reasons + ["no card written; the rule keeps trading unfiltered"],
            windows=windows,
            summary=summary,
        )
    return LedgerVerdict(
        status=VERDICT_PROMOTABLE,
        reasons=[
            f"beat taking every trade in all {len(windows)} windows by at least "
            f"{MIN_IMPROVEMENT:.0%}, AUC >= {MIN_AUC}, and stayed positive at 2x costs"
        ],
        windows=windows,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# the card and the runtime gate
# ---------------------------------------------------------------------------


@dataclass
class LedgerDecision:
    take: bool
    probability: float
    threshold: float
    reason: str


@dataclass
class LedgerGate:
    """A fitted gate plus the vocabulary it was fitted on, and how old it is.

    A card is a statement about a market regime. Left in place for long enough it becomes
    a statement about a market that no longer exists - and it is worse than no card,
    because it keeps refusing trades with confident-looking reasons. `stale_reason` is set
    by `load_card` when the card is older than the configured limit, and a stale gate is
    not ready, so the filter lets everything through and says why.
    """

    model: LogisticGate
    vocabulary: Vocabulary
    threshold: float
    meta: dict[str, Any] = field(default_factory=dict)
    stale_reason: str = ""

    @property
    def ready(self) -> bool:
        return (
            self.model.ready
            and bool(self.vocabulary.strategies)
            and not self.stale_reason
        )

    def age_days(self, now: datetime | None = None) -> float | None:
        created = _when(self.meta.get("created_at"))
        if created is None:
            return None
        return ((now or utcnow()) - created).total_seconds() / 86_400

    def decide(self, features: dict[str, float], *, strategy: str, symbol: str, moment: datetime) -> LedgerDecision:
        if not self.ready:
            return LedgerDecision(
                True,
                float("nan"),
                self.threshold,
                self.stale_reason or "ledger gate not ready",
            )
        if strategy not in self.vocabulary.strategies:
            return LedgerDecision(True, float("nan"), self.threshold, f"{strategy} was never in the ledger")
        if symbol not in self.vocabulary.symbols:
            return LedgerDecision(True, float("nan"), self.threshold, f"{symbol} was never in the ledger")
        vector, _ = self.vector(features, strategy, symbol, moment)
        probability = float(self.model.predict_proba(np.asarray([vector], dtype=float))[0])
        if np.isnan(probability):  # pragma: no cover - ready implies this is finite
            return LedgerDecision(True, probability, self.threshold, "ledger gate produced no score")
        take = probability >= self.threshold
        return LedgerDecision(
            take,
            probability,
            self.threshold,
            (
                f"ledger P(win) {probability:.3f} vs {self.threshold:.2f}"
                if not take
                else f"ledger P(win) {probability:.3f} clears {self.threshold:.2f}"
            ),
        )

    def vector(
        self, features: dict[str, float], strategy: str, symbol: str, moment: datetime
    ) -> tuple[list[float], bool]:
        row = [
            float(features.get(key, 0.0) or 0.0)
            for key in ("atr_bps", "stop_bps", "reward_bps", "rr")
        ]
        row += [1.0 if name == strategy else 0.0 for name in self.vocabulary.strategies]
        row += [1.0 if name == symbol else 0.0 for name in self.vocabulary.symbols]
        bucket = hour_bucket(moment)
        row += [1.0 if bucket == f"h{index}" else 0.0 for index in range(HOUR_BUCKETS)]
        return row, True


def fit_gate(
    trades: list[LedgerTrade],
    *,
    vocabulary: Vocabulary | None = None,
) -> LedgerGate:
    """Fit the final gate on every trade in the ledger (after walk-forward has judged it)."""
    vocabulary = vocabulary or Vocabulary.from_trades(trades)
    matrix = design_matrix(trades, vocabulary)
    labels = np.asarray([1.0 if trade.won else 0.0 for trade in trades])
    model = LogisticGate(alpha=1.0).fit(matrix, labels)
    outcomes = np.asarray([trade.net_r for trade in trades])
    threshold = _threshold(model.predict_proba(matrix), outcomes) if model.ready else 0.5
    return LedgerGate(model=model, vocabulary=vocabulary, threshold=threshold)


def save_card(path: str | Path, gate: LedgerGate, verdict: LedgerVerdict, *, report: LedgerReport | None = None) -> Path:
    """Write the card. Refuses to write one that has not earned promotion."""
    if not verdict.may_write_card:
        raise ValueError(f"refusing to write a card for a {verdict.status} verdict")
    if not gate.ready:
        raise ValueError("refusing to write a card for an unfit gate")
    assert gate.model.coef_ is not None
    assert gate.model.mean_ is not None
    assert gate.model.scale_ is not None
    payload = {
        "kind": "ledger_gate",
        "created_at": utcnow().isoformat(),
        "features": gate.vocabulary.names(),
        "vocabulary": gate.vocabulary.as_dict(),
        "coefficients": [float(c) for c in gate.model.coef_],
        "mean": [float(m) for m in gate.model.mean_],
        "scale": [float(s) for s in gate.model.scale_],
        "threshold": float(gate.threshold),
        "verdict": verdict.as_dict(),
        "ledger": report.summary() if report is not None else {},
        "contract": "veto-only: a card may remove a trade, never add or resize one",
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str))
    return target


def load_card(path: str | Path, *, max_age_days: float | None = None) -> LedgerGate:
    """Read a card. `max_age_days` is not a formality: a stale card stops vetoing."""
    payload = json.loads(Path(path).read_text())
    if payload.get("kind") != "ledger_gate":
        raise ValueError(f"{path} is not a ledger-gate card")
    model = LogisticGate()
    model.coef_ = np.asarray(payload.get("coefficients") or [], dtype=float)
    model.mean_ = np.asarray(payload.get("mean") or [], dtype=float)
    model.scale_ = np.asarray(payload.get("scale") or [], dtype=float)
    vocabulary = Vocabulary(
        strategies=list((payload.get("vocabulary") or {}).get("strategies") or []),
        symbols=list((payload.get("vocabulary") or {}).get("symbols") or []),
    )
    gate = LedgerGate(
        model=model,
        vocabulary=vocabulary,
        threshold=float(payload.get("threshold") or 0.5),
        meta={"created_at": payload.get("created_at"), "verdict": payload.get("verdict") or {}},
    )
    if max_age_days is not None and max_age_days > 0:
        age = gate.age_days()
        if age is None:
            gate.stale_reason = "card has no creation date, so its age cannot be checked"
        elif age > max_age_days:
            gate.stale_reason = (
                f"card is {age:.1f} days old, limit {max_age_days:.1f}: "
                "re-run `tradebot learn` on recent trades"
            )
    return gate


@dataclass
class LedgerEntryFilter:
    """The engine's veto-only entry hook, backed by a ledger card.

    It reads only what the signal and the strategy context carry *at decision time*: the
    ATR the strategy saw, the stop and target it chose, the hour, and the symbol. Anything
    missing, unfamiliar or broken lets the trade through and says why - a filter that
    silently stops trading is indistinguishable from a dead feed.
    """

    gate: LedgerGate
    name: str = "ledger_gate"
    vetoes: int = 0
    errors: int = 0

    def __call__(self, signal: Any, ctx: Any = None) -> tuple[bool, str]:
        if not self.gate.ready:
            return True, self.gate.stale_reason or "ledger gate not ready"
        try:
            mark = float(getattr(ctx, "mark", 0) or 0)
            if mark <= 0:
                return True, "no mark price for the ledger gate"
            atr = float(getattr(ctx, "atr", 0.0) or 0.0)
            stop = float(getattr(signal, "stop", 0) or 0)
            target = float(getattr(signal, "target", 0) or 0)
            stop_bps = abs(mark - stop) / mark * 10_000
            reward_bps = abs(target - mark) / mark * 10_000
            features = {
                "atr_bps": atr / mark * 10_000,
                "stop_bps": stop_bps,
                "reward_bps": reward_bps,
                "rr": (reward_bps / stop_bps) if stop_bps else 0.0,
            }
            decision = self.gate.decide(
                features,
                strategy=str(signal.strategy),
                symbol=str(signal.symbol),
                moment=signal.ts,
            )
        except Exception as exc:  # noqa: BLE001 - a broken card must not stop trading
            self.errors += 1
            log.warning(
                '{"event":"ledger_gate_error","error":"%s"}', type(exc).__name__
            )
            return True, f"ledger gate error: {type(exc).__name__}"
        if decision.take:
            return True, decision.reason
        self.vetoes += 1
        return False, decision.reason

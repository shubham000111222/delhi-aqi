"""The signal library (§19.1): about forty candidate signals and the screen that kills most.

A signal is a function of the features that returns a score in [-1, 1] per bar. It is
never allowed to look at a price after its own bar; that is the feature store's job, and
`screens` re-checks it with a time-shift test.

The important half of this module is the *screen*. Anyone can write forty signals; the
discipline is throwing away the ones that do not carry information, and doing it the same
way every time:

* **information coefficient** — rank correlation between today's score and the forward
  return. Mean and stability across the window's two halves both have to agree in sign.
* **cost-adjusted standalone PnL** — a signal whose edge disappears after fees is not an
  edge, and this account charges 10 bps a side.
* **correlation pruning** — among signals correlated above 0.8, the weaker one goes. Ten
  copies of the same trend factor are one signal with ten chances to look good.
* **shuffled-label check** — after shuffling forward returns, IC must collapse towards
  zero. If it does not, the pipeline is leaking.

The output is deliberately blunt: a list of signals that survived, and the reason each of
the others died.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from .features import Array, FeatureFrame, forward_returns, tanh_score

EPS = 1e-12
#: Round-trip cost assumption for standalone signal PnL, in return units (10 bps a side).
DEFAULT_FEE_PER_SIDE = 0.001


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def get(frame: FeatureFrame, name: str) -> Array:
    values = frame.features.get(name)
    if values is None:
        return np.full(len(frame), np.nan)
    return values


def nan_safe(values: Array) -> Array:
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


def combine(*parts: tuple[Array, float]) -> Array:
    """Weighted sum of score components, each already in [-1, 1]."""
    total = np.zeros(len(parts[0][0])) if parts else np.array([])
    weight_sum = 0.0
    for values, weight in parts:
        total = total + nan_safe(values) * weight
        weight_sum += abs(weight)
    return total / weight_sum if weight_sum else total


def rank_correlation(a: Array, b: Array) -> float:
    """Spearman correlation over the rows where both are finite."""
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 10:
        return 0.0
    x = _ranks(a[mask])
    y = _ranks(b[mask])
    if np.std(x) <= EPS or np.std(y) <= EPS:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _ranks(values: Array) -> Array:
    order = np.argsort(values, kind="mergesort")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(len(values), dtype=float)
    # average ties so the correlation is a true Spearman
    _, inverse, counts = np.unique(values, return_inverse=True, return_counts=True)
    if (counts > 1).any():
        sums = np.zeros(len(counts))
        np.add.at(sums, inverse, ranks)
        ranks = (sums / counts)[inverse]
    return ranks


def rank_ic(score: Array, target: Array, horizon: int) -> float:
    """Rank correlation on non-overlapping samples.

    Forward returns over a horizon overlap heavily bar to bar. Correlating every bar with
    a window that spans the next ``horizon`` bars counts the same move ``horizon`` times,
    which inflates the IC and shrinks its standard error to fiction. Sampling every
    ``horizon``-th row costs sample size and buys honesty.
    """
    stride = max(1, int(horizon))
    return rank_correlation(score[::stride], target[::stride])


def non_overlapping_samples(frame: FeatureFrame, horizon: int) -> int:
    """How many independent observations the IC is actually based on."""
    target = forward_returns(frame.close, horizon)
    return int(np.sum(~np.isnan(target))) // max(1, horizon)


def sampling_noise_band(n_samples: int) -> float:
    """95% band for a correlation that should be zero at this sample size."""
    return float(1.96 / np.sqrt(max(10, n_samples)))


def pearson(a: Array, b: Array) -> float:
    mask = ~(np.isnan(a) | np.isnan(b))
    if mask.sum() < 10:
        return 0.0
    x, y = a[mask], b[mask]
    if np.std(x) <= EPS or np.std(y) <= EPS:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


# ---------------------------------------------------------------------------
# signal definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalSpec:
    name: str
    family: str
    inputs: tuple[str, ...]
    lookback: int
    fn: Callable[[FeatureFrame], Array]
    version: str = "1.0"
    #: Long-only books skip negative scores instead of going short.
    long_only: bool = False

    def compute(self, frame: FeatureFrame) -> Array:
        score = np.asarray(self.fn(frame), dtype=float)
        if len(score) != len(frame):
            score = np.resize(score, len(frame))
        score = np.clip(score, -1.0, 1.0)
        if self.long_only:
            score = np.where(score > 0, score, 0.0)
        return score

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "inputs": list(self.inputs),
            "lookback": self.lookback,
            "version": self.version,
        }


def _fade(frame: FeatureFrame, name: str, strength: float = 0.5) -> Array:
    """Mean reversion: fade an extended reading."""
    return tanh_score(-get(frame, name) * strength)


def _follow(frame: FeatureFrame, name: str, strength: float = 0.5) -> Array:
    return tanh_score(get(frame, name) * strength)


def _gated(frame: FeatureFrame, signal: str, gate: str, threshold: float) -> Array:
    """Only express a signal when a regime gate is on, else flat."""
    scores = get(frame, signal)
    gate_values = get(frame, gate)
    on = nan_safe(gate_values) > threshold
    return np.where(on, nan_safe(scores), 0.0)


SIGNALS: tuple[SignalSpec, ...] = (
    # --- trend -------------------------------------------------------------
    SignalSpec("trend_ema_slope_20", "trend", ("ema_slope_20",), 40,
               lambda f: _follow(f, "ema_slope_20", 0.8)),
    SignalSpec("trend_ema_slope_50", "trend", ("ema_slope_50",), 100,
               lambda f: _follow(f, "ema_slope_50", 0.8)),
    SignalSpec("trend_donchian_20", "trend", ("donchian_pos_20",), 20,
               lambda f: _follow(f, "donchian_pos_20", 1.2)),
    SignalSpec("trend_donchian_55", "trend", ("donchian_pos_55",), 55,
               lambda f: _follow(f, "donchian_pos_55", 1.2)),
    SignalSpec("trend_macd", "trend", ("macd_hist_12_26",), 60,
               lambda f: _follow(f, "macd_hist_12_26", 0.7)),
    SignalSpec("trend_price_vs_sma50", "trend", ("price_vs_sma_50",), 50,
               lambda f: _follow(f, "price_vs_sma_50", 0.8)),
    SignalSpec("trend_adx_direction", "trend", ("adx_direction",), 40,
               lambda f: _follow(f, "adx_direction", 1.0)),
    SignalSpec("trend_momentum_5", "trend", ("ret_5",), 6,
               lambda f: _follow(f, "ret_5", 0.8)),
    SignalSpec("trend_momentum_20", "trend", ("ret_20",), 21,
               lambda f: _follow(f, "ret_20", 0.8)),
    SignalSpec("trend_ema_cross_state", "trend", ("ema_slope_20", "ema_slope_50"), 100,
               lambda f: tanh_score(nan_safe(get(f, "ema_slope_20"))
                                     - nan_safe(get(f, "ema_slope_50")))),
    # --- mean reversion ----------------------------------------------------
    SignalSpec("mr_rsi_14", "mean_reversion", ("rsi_distance_14",), 14,
               lambda f: _fade(f, "rsi_distance_14", 1.2)),
    SignalSpec("mr_rsi_7", "mean_reversion", ("rsi_distance_7",), 7,
               lambda f: _fade(f, "rsi_distance_7", 1.0)),
    SignalSpec("mr_bb_z_20", "mean_reversion", ("bb_z_20",), 20,
               lambda f: _fade(f, "bb_z_20", 1.0)),
    SignalSpec("mr_vwap_20", "mean_reversion", ("vwap_distance_20",), 20,
               lambda f: _fade(f, "vwap_distance_20", 1.0)),
    SignalSpec("mr_close_z_20", "mean_reversion", ("close_z_20",), 20,
               lambda f: _fade(f, "close_z_20", 0.8)),
    SignalSpec("mr_ret_1", "mean_reversion", ("ret_1",), 2,
               lambda f: _fade(f, "ret_1", 1.0)),
    SignalSpec("mr_bb_high_vol_only", "mean_reversion",
               ("bb_z_20", "realized_vol_24"), 40,
               lambda f: _gated(f, "mr_bb_z_20", "realized_vol_24", 0.0)),
    SignalSpec("mr_rsi_low_vol_only", "mean_reversion", ("rsi_distance_14", "atr_pct_14"), 40,
               lambda f: _gated(f, "mr_rsi_14", "atr_pct_14", -0.2)),
    # --- volatility --------------------------------------------------------
    SignalSpec("vol_regime_trend", "volatility", ("ema_slope_20", "vol_of_vol_24"), 48,
               lambda f: _gated(f, "trend_ema_slope_20", "vol_of_vol_24", -0.5)),
    SignalSpec("vol_regime_fade", "volatility", ("bb_z_20", "atr_percentile_96"), 110,
               lambda f: _gated(f, "mr_bb_z_20", "atr_percentile_96", 0.5)),
    SignalSpec("vol_compression_breakout", "volatility",
               ("donchian_pos_20", "range_compression"), 60,
               lambda f: _gated(f, "trend_donchian_20", "range_compression", 0.5)),
    SignalSpec("vol_expansion_reversal", "volatility", ("ret_1", "bb_width_20"), 40,
               lambda f: _gated(f, "mr_ret_1", "bb_width_20", 0.0)),
    SignalSpec("vol_atr_percentile", "volatility", ("atr_percentile_96",), 110,
               lambda f: tanh_score(-get(f, "atr_percentile_96") * 0.5)),
    SignalSpec("vol_of_vol_risk_off", "volatility", ("vol_of_vol_24",), 48,
               lambda f: tanh_score(-get(f, "vol_of_vol_24") * 0.4)),
    # --- volume and flow ---------------------------------------------------
    SignalSpec("flow_volume_z", "volume", ("volume_z_20",), 20,
               lambda f: tanh_score(get(f, "volume_z_20") * 0.3)),
    SignalSpec("flow_obv_slope", "volume", ("obv_slope_24",), 48,
               lambda f: _follow(f, "obv_slope_24", 0.6)),
    SignalSpec("flow_taker_ratio", "volume", ("taker_buy_ratio_20",), 20,
               lambda f: tanh_score(get(f, "taker_buy_ratio_20") * 0.6)),
    SignalSpec("flow_dollar_volume", "volume", ("dollar_volume_z_20",), 20,
               lambda f: tanh_score(-get(f, "dollar_volume_z_20") * 0.2)),
    SignalSpec("flow_price_volume_confirm", "volume",
               ("ret_5", "volume_z_20"), 20,
               lambda f: tanh_score(get(f, "ret_5") * np.sign(nan_safe(get(f, "volume_z_20"))))),
    # --- order book --------------------------------------------------------
    SignalSpec("book_imbalance_5", "order_book", ("book_imbalance_5",), 1,
               lambda f: tanh_score(get(f, "book_imbalance_5") * 2.0)),
    SignalSpec("book_imbalance_10", "order_book", ("book_imbalance_10",), 1,
               lambda f: tanh_score(get(f, "book_imbalance_10") * 2.0)),
    SignalSpec("book_spread_penalty", "order_book", ("book_spread_bps",), 1,
               lambda f: tanh_score(-nan_safe(get(f, "book_spread_bps")) / 10.0)),
    SignalSpec("book_depth_ratio", "order_book", ("book_depth_ratio",), 1,
               lambda f: tanh_score(np.log(np.abs(get(f, "book_depth_ratio")) + 1e-9) * 0.3)),
    SignalSpec("book_microprice", "order_book", ("microprice_offset",), 1,
               lambda f: tanh_score(-get(f, "microprice_offset") * 500.0)),
    SignalSpec("book_imbalance_with_trend", "order_book",
               ("book_imbalance_10", "ema_slope_20"), 40,
               lambda f: combine((get(f, "book_imbalance_10"), 2.0),
                                 (tanh_score(get(f, "ema_slope_20")), 1.0))),
    # --- cross asset -------------------------------------------------------
    SignalSpec("xasset_btc_strength", "cross_asset", ("btc_relative_strength",), 20,
               lambda f: _follow(f, "btc_relative_strength", 1.0)),
    SignalSpec("xasset_btc_correlation_shift", "cross_asset", ("btc_correlation_96",), 96,
               lambda f: tanh_score(get(f, "btc_correlation_96") * 0.5)),
    SignalSpec("xasset_btc_fade", "cross_asset",
               ("btc_relative_strength", "close_z_20"), 40,
               lambda f: combine((tanh_score(-get(f, "btc_relative_strength")), 1.0),
                                 (tanh_score(-get(f, "close_z_20")), 0.5))),
    # --- time --------------------------------------------------------------
    SignalSpec("time_hour_of_day", "time", ("hour_of_day",), 0,
               lambda f: tanh_score(-get(f, "hour_of_day") * 0.2)),
    SignalSpec("time_weekend", "time", ("weekend_flag",), 0,
               lambda f: np.where(nan_safe(get(f, "weekend_flag")) > 0, -0.5, 0.0)),
    # --- combinations ------------------------------------------------------
    SignalSpec("combo_trend_and_flow", "combo", ("ema_slope_20", "obv_slope_24"), 48,
               lambda f: combine((tanh_score(get(f, "ema_slope_20")), 1.0),
                                 (tanh_score(get(f, "obv_slope_24")), 1.0))),
    SignalSpec("combo_trend_and_volume", "combo", ("macd_hist_12_26", "volume_z_20"), 60,
               lambda f: combine((tanh_score(get(f, "macd_hist_12_26")), 1.0),
                                 (tanh_score(get(f, "volume_z_20")), 0.5))),
    SignalSpec("combo_fade_and_lowvol", "combo", ("bb_z_20", "atr_pct_14"), 40,
               lambda f: combine((tanh_score(-get(f, "bb_z_20")), 1.0),
                                 (tanh_score(-get(f, "atr_pct_14")), 0.5))),
    SignalSpec("combo_breakout_and_depth", "combo",
               ("donchian_pos_20", "book_imbalance_10"), 40,
               lambda f: combine((tanh_score(get(f, "donchian_pos_20")), 1.0),
                                 (tanh_score(get(f, "book_imbalance_10") * 2.0), 1.0))),
)


def library_by_family() -> dict[str, list[SignalSpec]]:
    out: dict[str, list[SignalSpec]] = {}
    for spec in SIGNALS:
        out.setdefault(spec.family, []).append(spec)
    return out


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


@dataclass
class SignalEvaluation:
    name: str
    family: str
    horizon: int
    ic: float
    ic_first_half: float
    ic_second_half: float
    hit_rate: float
    turnover: float
    net_pnl: float
    gross_pnl: float
    coverage: float
    ic_by_horizon: dict[int, float] = field(default_factory=dict)
    #: all PnL figures are per bar, net of `fee_per_side` per unit of position change
    #: number of non-overlapping observations behind the IC
    samples: int = 0

    @property
    def stable(self) -> bool:
        """Same sign in both halves, and neither half is negligible."""
        return (
            self.ic_first_half * self.ic_second_half > 0
            and abs(self.ic_first_half) > 0.005
            and abs(self.ic_second_half) > 0.005
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "horizon": self.horizon,
            "ic": round(self.ic, 5),
            "ic_first_half": round(self.ic_first_half, 5),
            "ic_second_half": round(self.ic_second_half, 5),
            "stable": self.stable,
            "hit_rate": round(self.hit_rate, 4),
            "turnover": round(self.turnover, 4),
            "net_pnl": round(self.net_pnl, 5),
            "gross_pnl": round(self.gross_pnl, 5),
            "coverage": round(self.coverage, 3),
            "ic_by_horizon": {str(k): round(v, 5) for k, v in self.ic_by_horizon.items()},
            "non_overlapping_samples": self.samples,
            "noise_band": round(sampling_noise_band(self.samples), 5),
        }


def evaluate(
    frame: FeatureFrame,
    spec: SignalSpec,
    *,
    horizons: Sequence[int] = (1, 4, 24),
    primary_horizon: int = 4,
    fee_per_side: float = DEFAULT_FEE_PER_SIDE,
) -> SignalEvaluation:
    """IC at each horizon, stability across halves, and cost-adjusted standalone PnL."""
    score = spec.compute(frame)
    close = frame.close
    ic_by_horizon: dict[int, float] = {}
    primary_returns = forward_returns(close, primary_horizon)
    # the primary horizon may not be in the default list; it must always be measured
    for horizon in sorted(set(horizons) | {primary_horizon}):
        ic_by_horizon[horizon] = rank_ic(score, forward_returns(close, horizon), horizon)

    usable = ~(np.isnan(score) | np.isnan(primary_returns))
    coverage = float(usable.sum()) / len(frame) if len(frame) else 0.0
    # stability is judged on the non-overlapping subsample, split in two
    strided = np.flatnonzero(usable)[:: max(1, primary_horizon)]
    if strided.size >= 20:
        half = strided.size // 2
        first, second = strided[:half], strided[half:]
        ic_first = rank_correlation(score[first], primary_returns[first])
        ic_second = rank_correlation(score[second], primary_returns[second])
    else:
        ic_first = ic_second = 0.0
    if usable.sum() >= 20:
        positions = np.where(usable, np.sign(score), 0.0)
        # per-bar numbers: a total would just scale with the length of the sample, and
        # a fee total would look catastrophic next to a gross total for no good reason
        gross = float(np.nansum(positions * np.nan_to_num(primary_returns))) / usable.sum()
        trades = float(np.nansum(np.abs(np.diff(np.concatenate([[0.0], positions])))))
        fees = trades * fee_per_side / usable.sum()
        hit = float(np.mean(np.sign(score[usable]) == np.sign(primary_returns[usable])))
    else:
        gross = fees = hit = 0.0
        positions = np.zeros(len(frame))
        trades = 0.0

    turnover = trades / max(1.0, float(usable.sum()))
    return SignalEvaluation(
        name=spec.name,
        family=spec.family,
        horizon=primary_horizon,
        ic=ic_by_horizon.get(primary_horizon, 0.0),
        ic_first_half=ic_first,
        ic_second_half=ic_second,
        hit_rate=hit,
        turnover=turnover,
        net_pnl=gross - fees,
        gross_pnl=gross,
        coverage=coverage,
        ic_by_horizon=ic_by_horizon,
        samples=non_overlapping_samples(frame, primary_horizon),
    )


# ---------------------------------------------------------------------------
# screening
# ---------------------------------------------------------------------------


@dataclass
class ScreenResult:
    kept: list[SignalSpec]
    dropped: list[dict[str, Any]]
    evaluations: dict[str, SignalEvaluation]
    correlations: dict[str, float]
    shuffled_ic: float
    samples: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kept": [s.name for s in self.kept],
            "dropped": self.dropped,
            "evaluations": {k: v.as_dict() for k, v in self.evaluations.items()},
            "correlations": {k: round(v, 4) for k, v in self.correlations.items()},
            "shuffled_ic": round(self.shuffled_ic, 5),
            "non_overlapping_samples": self.samples,
            "noise_band": round(sampling_noise_band(self.samples), 5),
        }

    def table_rows(self) -> list[dict[str, Any]]:
        rows = []
        kept_names = {s.name for s in self.kept}
        band = sampling_noise_band(self.samples)
        for name, evaluation in sorted(self.evaluations.items(), key=lambda kv: -abs(kv[1].ic)):
            row = evaluation.as_dict()
            row["kept"] = name in kept_names
            # "survived the screen" and "is statistically distinguishable from noise" are
            # different claims, and a short sample is usually the reason they differ
            row["distinguishable"] = abs(evaluation.ic) > band
            rows.append(row)
        return rows


def screen(
    frame: FeatureFrame,
    *,
    specs: Iterable[SignalSpec] | None = None,
    primary_horizon: int = 4,
    min_ic: float = 0.01,
    max_correlation: float = 0.8,
    fee_per_side: float = DEFAULT_FEE_PER_SIDE,
    seed: int = 7,
) -> ScreenResult:
    """Keep only signals with a stable, cost-surviving, non-redundant edge."""
    candidates = list(specs or SIGNALS)
    evaluations: dict[str, SignalEvaluation] = {}
    dropped: list[dict[str, Any]] = []

    for spec in candidates:
        try:
            evaluation = evaluate(
                frame, spec, primary_horizon=primary_horizon, fee_per_side=fee_per_side
            )
        except (ValueError, IndexError):  # pragma: no cover - defensive
            dropped.append({"name": spec.name, "reason": "failed to compute"})
            continue
        evaluations[spec.name] = evaluation
        if evaluation.coverage < 0.2:
            dropped.append(
                {"name": spec.name, "reason": f"too little data ({evaluation.coverage:.0%} coverage)"}
            )
        elif abs(evaluation.ic) < min_ic:
            dropped.append(
                {"name": spec.name, "reason": f"IC {evaluation.ic:+.3f} is indistinguishable from zero"}
            )
        elif not evaluation.stable:
            dropped.append(
                {
                    "name": spec.name,
                    "reason": (
                        f"unstable: IC {evaluation.ic_first_half:+.3f} in the first half, "
                        f"{evaluation.ic_second_half:+.3f} in the second"
                    ),
                }
            )
        elif evaluation.net_pnl <= 0:
            dropped.append(
                {
                    "name": spec.name,
                    "reason": (
                        f"cost-adjusted PnL {evaluation.net_pnl:+.3f} <= 0 "
                        f"(gross {evaluation.gross_pnl:+.3f}, fees ate the rest)"
                    ),
                }
            )

    survivors = [s for s in candidates if s.name in evaluations and
                 s.name not in {d["name"] for d in dropped}]
    # correlation pruning, strongest IC first
    survivors.sort(key=lambda s: -abs(evaluations[s.name].ic))
    kept: list[SignalSpec] = []
    correlations: dict[str, float] = {}
    for spec in survivors:
        scores = spec.compute(frame)
        redundant_with = None
        for chosen in kept:
            rho = pearson(scores, chosen.compute(frame))
            if abs(rho) > max_correlation:
                correlations[f"{spec.name}~{chosen.name}"] = rho
                redundant_with = chosen.name
                break
        if redundant_with is not None:
            dropped.append(
                {
                    "name": spec.name,
                    "reason": (
                        f"redundant with {redundant_with} "
                        f"(correlation {correlations[f'{spec.name}~{redundant_with}']:+.2f} "
                        f"> {max_correlation})"
                    ),
                }
            )
        else:
            kept.append(spec)

    shuffled = _shuffled_check(frame, kept, primary_horizon, seed)
    return ScreenResult(
        kept=kept,
        dropped=dropped,
        evaluations=evaluations,
        correlations=correlations,
        shuffled_ic=shuffled,
        samples=non_overlapping_samples(frame, primary_horizon),
    )


def _shuffled_check(
    frame: FeatureFrame, kept: Sequence[SignalSpec], horizon: int, seed: int
) -> float:
    """Mean IC of the surviving signals against shuffled forward returns.

    It must collapse to roughly zero. A large value here would mean the frame or the
    evaluation is leaking future information, and the honest response is to look at the
    pipeline rather than the signals.
    """
    if not kept:
        return 0.0
    rng = np.random.default_rng(seed)
    returns = forward_returns(frame.close, horizon)
    finite = np.flatnonzero(~np.isnan(returns))
    shuffled = np.full(len(returns), np.nan)
    shuffled[finite] = rng.permutation(returns[finite])
    values = [rank_ic(spec.compute(frame), shuffled, horizon) for spec in kept]
    return float(np.mean(values)) if values else 0.0


def signal_correlation_matrix(frame: FeatureFrame, specs: Sequence[SignalSpec]) -> dict[str, dict[str, float]]:
    scores = {spec.name: spec.compute(frame) for spec in specs}
    names = list(scores)
    matrix: dict[str, dict[str, float]] = {}
    for a in names:
        matrix[a] = {b: round(pearson(scores[a], scores[b]), 4) for b in names}
    return matrix


def decay_curve(signals: dict[str, Array], close: Array, horizons: Sequence[int] = (1, 2, 4, 8, 24)) -> dict[str, dict[int, float]]:
    """IC by horizon for a set of scores: the decay of whatever edge exists."""
    out: dict[str, dict[int, float]] = {}
    for name, score in signals.items():
        out[name] = {h: rank_correlation(score, forward_returns(close, h)) for h in horizons}
    return out


def ic_weighted_score(frame: FeatureFrame, specs: Sequence[SignalSpec], *, allow_short: bool = True) -> Array:
    """Baseline level 1 of the complexity ladder: IC-weighted average of the signals."""
    if not specs:
        return np.zeros(len(frame))
    parts: list[tuple[Array, float]] = []
    for spec in specs:
        evaluation = evaluate(frame, spec)
        if math.isnan(evaluation.ic):

            continue
        parts.append((spec.compute(frame), evaluation.ic))
    if not parts:
        return np.zeros(len(frame))
    total = np.zeros(len(frame))
    weight_sum = 0.0
    for scores, weight in parts:
        total = total + nan_safe(scores) * weight
        weight_sum += abs(weight)
    out = total / weight_sum if weight_sum else total
    out = np.clip(out, -1.0, 1.0)
    return out if allow_short else np.clip(out, 0.0, 1.0)


def to_timestamped(frame: FeatureFrame, scores: Array, name: str) -> list[dict[str, Any]]:
    """Signal output in the shape the engine/report layer wants: score plus time."""
    rows = []
    for ts, value in zip(frame.ts, scores, strict=False):
        if np.isnan(value):
            continue
        rows.append({"signal": name, "ts": ts.isoformat(), "score": round(float(value), 4)})
    return rows


def latest_scores(frame: FeatureFrame, specs: Sequence[SignalSpec]) -> list[dict[str, Any]]:
    """The most recent bar's score per signal, for the dashboard and daily report."""
    out = []
    for spec in specs:
        score = spec.compute(frame)
        finite = np.flatnonzero(~np.isnan(score))
        if finite.size == 0:
            continue
        index = int(finite[-1])
        out.append(
            {
                "signal": spec.name,
                "family": spec.family,
                "ts": frame.ts[index].isoformat(),
                "score": round(float(score[index]), 4),
            }
        )
    return sorted(out, key=lambda row: -abs(row["score"]))


def timestamps_of(frame: FeatureFrame) -> list[datetime]:
    return list(frame.ts)

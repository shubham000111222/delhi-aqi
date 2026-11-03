"""Parameter sensitivity: does the edge survive small changes, or is it a spike?

The plan asks for heatmaps and for "broad plateaus, not isolated peaks". A strategy
tuned to one magic number is fitted to noise: nudge the parameter and the result
collapses. This module runs a grid, then judges the *neighbourhood* of the best cell
rather than the best cell alone.

The judgement is deliberately blunt:

* **plateau share** — how much of the grid is within `tolerance` of the best score.
  A broad plateau is mostly-high everywhere; a spike is one good cell in a sea of bad.
* **isolation** — the best cell compared with the median of its immediate neighbours.
  If the best is far above everything around it, that is a fitting artefact.

Neither number proves an edge; both reliably catch a mirage.
"""

from __future__ import annotations

import copy
import itertools
import statistics
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..config import Config
from ..core.money import to_str
from .walkforward import SegmentResult


def grid_points(axes: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of ``{"param": [values...]}`` in a stable order."""
    if not axes:
        return [{}]
    names = list(axes)
    points: list[dict[str, Any]] = []
    for combo in itertools.product(*(axes[n] for n in names)):
        points.append(dict(zip(names, combo, strict=True)))
    return points


def grid_key(params: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(sorted(params.items()))


@dataclass
class Cell:
    params: dict[str, Any]
    score: float
    trades: int
    end_equity: Decimal | None = None
    error: str | None = None

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {**self.params, "score": round(self.score, 4), "trades": self.trades}
        if self.end_equity is not None:
            row["end_equity"] = to_str(self.end_equity, 2)
        if self.error:
            row["error"] = self.error
        return row


@dataclass
class GridResult:
    cells: list[Cell] = field(default_factory=list)
    axes: dict[str, Sequence[Any]] = field(default_factory=dict)

    def best(self) -> Cell | None:
        usable = [c for c in self.cells if c.error is None]
        return max(usable, key=lambda c: c.score) if usable else None

    def lookup(self, params: dict[str, Any]) -> Cell | None:
        key = grid_key(params)
        for cell in self.cells:
            if grid_key(cell.params) == key:
                return cell
        return None


def neighbours(
    grid: GridResult, params: dict[str, Any]
) -> list[Cell]:
    """Cells one step away along exactly one axis (diagonals excluded)."""
    out: list[Cell] = []
    for axis, values in grid.axes.items():
        if axis not in params:
            continue
        positions = list(values)
        try:
            index = positions.index(params[axis])
        except ValueError:  # pragma: no cover - defensive
            continue
        for delta in (-1, 1):
            target = index + delta
            if 0 <= target < len(positions):
                candidate = {**params, axis: positions[target]}
                cell = grid.lookup(candidate)
                if cell is not None and cell.error is None:
                    out.append(cell)
    return out


def plateau_report(
    grid: GridResult,
    *,
    tolerance: float = 0.25,
    isolation_ratio: float = 2.0,
) -> dict[str, Any]:
    """Judge the grid's shape around its best cell.

    ``tolerance`` is the fractional band around the best score that still counts as
    "good" (0.25 means a cell within 25% of the best). ``isolation_ratio`` is how many
    times better than its neighbours' median the best cell must be to count as a spike.
    """
    usable = [c for c in grid.cells if c.error is None]
    best = grid.best()
    if best is None or not usable:
        return {
            "cells": len(grid.cells),
            "failed_cells": sum(1 for c in grid.cells if c.error is not None),
            "verdict": "no usable cells",
            "isolated_peak": True,
            "plateau_share": 0.0,
        }

    scores = [c.score for c in usable]
    band = abs(best.score) * tolerance if best.score else tolerance
    within = sum(1 for s in scores if best.score - s <= band)
    around = neighbours(grid, best.params)
    neighbour_scores = [c.score for c in around]
    neighbour_median = statistics.median(neighbour_scores) if neighbour_scores else best.score

    isolated = False
    if neighbour_scores:
        clearly_better = neighbour_median > 0 and best.score / neighbour_median >= isolation_ratio
        if (neighbour_median <= 0 < best.score) or clearly_better:
            isolated = True

    share = within / len(usable)
    if best.score <= 0:
        # A wide plateau of losses is still a loss: shape never rescues a negative edge.
        verdict = "no edge - best cell is not profitable"
    elif isolated:
        verdict = "isolated peak - do not promote"
    elif share >= 0.4:
        verdict = "broad plateau"
    else:
        verdict = "narrow plateau - treat with suspicion"

    return {
        "cells": len(grid.cells),
        "usable_cells": len(usable),
        "failed_cells": len(grid.cells) - len(usable),
        "best": best.as_row(),
        "best_neighbours": len(neighbour_scores),
        "neighbour_median": round(neighbour_median, 4),
        "plateau_share": round(share, 3),
        "tolerance": tolerance,
        "isolated_peak": isolated,
        "median_score": round(statistics.median(scores), 4),
        "worst_score": round(min(scores), 4),
        "verdict": verdict,
    }


#: ``(params, config) -> SegmentResult``: inject a runner in tests.
GridRunner = Callable[[dict[str, Any], Config], Awaitable[SegmentResult]]


async def run_grid(
    config: Config,
    *,
    strategy: str,
    axes: Mapping[str, Sequence[Any]],
    start: datetime,
    end: datetime,
    capital: Decimal | None = None,
    seed: int | None = None,
    metric: str = "net_return",
    runner: GridRunner | None = None,
) -> dict[str, Any]:
    """Run one engine segment per grid point and judge the shape of the results."""
    if runner is None:
        runner = _default_runner(start=start, end=end, capital=capital, seed=seed)

    grid = GridResult(axes=dict(axes))
    for params in grid_points(axes):
        cfg = copy.deepcopy(config)
        entry = cfg.strategies.strategies.get(strategy)
        if entry is None:
            raise KeyError(f"unknown strategy {strategy!r}")
        entry.params.update(params)
        try:
            result = await runner(params, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad cell must not lose the grid
            grid.cells.append(Cell(params=params, score=float("nan"), trades=0, error=str(exc)[:200]))
            continue
        score = _score(result, metric)
        grid.cells.append(
            Cell(params=params, score=score, trades=len(result.trades), end_equity=result.end_equity)
        )

    # NaN scores cannot be compared, so drop them before the shape analysis.
    clean = GridResult(axes=dict(axes), cells=[c for c in grid.cells if c.error is None])
    report = plateau_report(clean)
    report.update(
        {
            "strategy": strategy,
            "metric": metric,
            "axes": {k: [str(v) for v in vals] for k, vals in axes.items()},
            "grid": [c.as_row() for c in grid.cells],
        }
    )
    return report


def _score(result: SegmentResult, metric: str) -> float:
    if metric == "net_return":
        return float(result.net_return)
    if metric == "profit_factor":
        gains = sum((float(t.pnl) for t in result.trades if t.pnl > 0), 0.0)
        losses = -sum((float(t.pnl) for t in result.trades if t.pnl < 0), 0.0)
        if losses == 0:
            return 99.0 if gains > 0 else 0.0
        return gains / losses
    if metric == "pnl":
        return sum(float(t.pnl) for t in result.trades)
    raise ValueError(f"unknown metric {metric!r}")


def _default_runner(
    *, start: datetime, end: datetime, capital: Decimal | None, seed: int | None
) -> GridRunner:
    from .walkforward import run_segment

    async def runner(params: dict[str, Any], cfg: Config) -> SegmentResult:  # noqa: ARG001
        return await run_segment(cfg, start, end, capital=capital, seed=seed)

    return runner


def format_sensitivity(report: dict[str, Any]) -> str:
    best = report.get("best") or {}
    lines = [
        f"parameter sensitivity: {report.get('strategy')} on {report.get('metric')}",
        f"cells {report.get('cells')} | usable {report.get('usable_cells', 0)} "
        f"| failed {report.get('failed_cells', 0)}",
        f"best {best.get('score')} at { {k: v for k, v in best.items() if k not in {'score', 'trades', 'end_equity', 'error'} } }",
        f"neighbour median {report.get('neighbour_median')} | plateau share "
        f"{report.get('plateau_share')} | isolated peak: {report.get('isolated_peak')}",
        f"verdict: {report.get('verdict')}",
    ]
    return "\n".join(lines)


def iter_axis_values(axes: dict[str, Iterable[Any]]) -> dict[str, list[Any]]:
    return {name: list(values) for name, values in axes.items()}

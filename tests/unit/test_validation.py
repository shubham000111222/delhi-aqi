"""Tests for the validation layer: walk-forward, sensitivity, tape and parity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradebot.backtest.parity import compare, normalise, run_parity
from tradebot.backtest.sensitivity import (
    Cell,
    GridResult,
    grid_points,
    neighbours,
    plateau_report,
)
from tradebot.backtest.walkforward import (
    WindowOutcome,
    purged_folds,
    rolling_windows,
    summarise,
)
from tradebot.core.money import D

START = datetime(2024, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------
class TestRollingWindows:
    def test_windows_roll_forward_without_overlapping_tests(self):
        windows = rolling_windows(
            START, START + timedelta(days=10), train_days=4, test_days=2
        )
        assert len(windows) == 3
        for window in windows:
            # the test set must always follow the training set
            assert window.train_end == window.test_start
            assert window.train_end > window.train_start
            assert window.test_end > window.test_start
        for left, right in zip(windows, windows[1:], strict=False):
            assert not left.overlaps(right), "consecutive test windows must not overlap"

    def test_window_that_does_not_fit_is_dropped(self):
        # 5 days of history, 4 train + 3 test: one window fits, the second does not
        windows = rolling_windows(START, START + timedelta(days=8), train_days=4, test_days=3)
        assert len(windows) == 1

    def test_bad_arguments_are_rejected(self):
        with pytest.raises(ValueError):
            rolling_windows(START, START, train_days=1, test_days=1)
        with pytest.raises(ValueError):
            rolling_windows(START, START + timedelta(days=5), train_days=0, test_days=1)


class TestPurgedFolds:
    def test_no_training_label_overlaps_the_test_block(self):
        times = [START + timedelta(hours=i) for i in range(400)]
        horizon = timedelta(hours=24)
        embargo = timedelta(hours=1)
        folds = purged_folds(times, n_folds=4, horizon=horizon, embargo=embargo)
        assert folds
        for train, test in folds:
            if not test:
                continue
            test_start, test_end = times[test[0]], times[test[-1]]
            for index in train:
                ts = times[index]
                # a training sample is only allowed if its label window is fully in the
                # past, or it sits beyond the embargo after the test block
                safe_before = ts + horizon < test_start
                safe_after = ts > test_end + embargo
                assert safe_before or safe_after, f"leaked sample at {ts}"

    def test_folds_cover_every_sample_exactly_once(self):
        times = [START + timedelta(hours=i) for i in range(100)]
        folds = purged_folds(times, n_folds=5, horizon=timedelta(hours=1), embargo=timedelta(0))
        seen = sorted(index for _, test in folds for index in test)
        assert seen == list(range(100))

    def test_single_fold_is_rejected(self):
        with pytest.raises(ValueError):
            purged_folds([START], n_folds=1)


class _FakeSegment:
    """Stands in for a real engine run, so the window logic is testable in milliseconds."""

    def __init__(self, net_return: float):
        self.net_return = D(str(net_return))
        self.trades = []
        self.curve = []
        self.start_equity = D("1000")
        self.end_equity = D("1000") * (D("1") + D(str(net_return)))

    def as_row(self):
        return {
            "start_equity": "1000",
            "end_equity": str(self.end_equity),
            "net_return": str(self.net_return),
            "trades": 0,
            "fees": "0",
        }


def _outcomes(returns: list[float]) -> list[WindowOutcome]:
    windows = rolling_windows(START, START + timedelta(days=100), train_days=1, test_days=1)
    return [
        WindowOutcome(window=windows[i], test=_FakeSegment(r))  # type: ignore[arg-type]
        for i, r in enumerate(returns)
    ]


class TestSummarise:
    def test_mostly_positive_windows_are_consistent(self):
        summary = summarise(_outcomes([0.01, 0.02, -0.005, 0.015]), D("1000"))
        assert summary["verdict"] == "consistent"
        assert summary["profitable_windows"] == 3
        assert summary["compound_return"] > 0

    def test_one_big_win_cannot_carry_a_losing_sequence(self):
        summary = summarise(_outcomes([-0.02, -0.03, 0.09, -0.04]), D("1000"))
        assert summary["profitable_share"] == 0.25
        assert summary["verdict"] == "inconsistent"

    def test_no_windows_is_not_a_pass(self):
        summary = summarise([], D("1000"))
        assert summary["windows"] == 0
        assert summary["verdict"] == "no windows"


# ---------------------------------------------------------------------------
# sensitivity
# ---------------------------------------------------------------------------
def _grid(scores: dict[tuple[int, int], float]) -> GridResult:
    axes = {"fast": [10, 20, 30], "slow": [40, 50, 60]}
    cells = [
        Cell(params={"fast": fast, "slow": slow}, score=score, trades=10)
        for (fast, slow), score in scores.items()
    ]
    return GridResult(cells=cells, axes=axes)


class TestSensitivity:
    def test_grid_is_the_cartesian_product(self):
        points = grid_points({"a": [1, 2], "b": [3, 4, 5]})
        assert len(points) == 6
        assert {"a": 1, "b": 3} in points

    def test_neighbours_are_one_step_along_one_axis(self):
        grid = _grid({(f, s): 0.01 for f in (10, 20, 30) for s in (40, 50, 60)})
        around = neighbours(grid, {"fast": 20, "slow": 50})
        assert len(around) == 4, "centre of a 3x3 grid has four orthogonal neighbours"

    def test_broad_plateau_is_accepted(self):
        grid = _grid(
            {(f, s): 0.02 + 0.001 * f for f in (10, 20, 30) for s in (40, 50, 60)}
        )
        report = plateau_report(grid)
        assert report["isolated_peak"] is False
        assert report["plateau_share"] > 0.5
        assert report["verdict"] == "broad plateau"

    def test_lone_spike_is_rejected(self):
        scores = {(f, s): 0.001 for f in (10, 20, 30) for s in (40, 50, 60)}
        scores[(20, 50)] = 0.5  # one magic cell surrounded by nothing
        report = plateau_report(_grid(scores), isolation_ratio=2.0)
        assert report["isolated_peak"] is True
        assert report["verdict"] == "isolated peak - do not promote"

    def test_all_negative_grid_is_not_rescued_by_tolerance(self):
        scores = {(f, s): -0.01 * f / 10 for f in (10, 20, 30) for s in (40, 50, 60)}
        report = plateau_report(_grid(scores))
        assert report["best"]["score"] < 0
        # a broad plateau of losses must never read as "broad plateau" and be promoted
        assert report["verdict"] == "no edge - best cell is not profitable"

    def test_failed_cells_are_reported_not_hidden(self):
        grid = GridResult(
            axes={"a": [1]},
            cells=[Cell(params={"a": 1}, score=0.1, trades=3), Cell(params={"a": 2}, score=float("nan"), trades=0, error="boom")],
        )
        report = plateau_report(grid)
        assert report["failed_cells"] == 1


# ---------------------------------------------------------------------------
# parity
# ---------------------------------------------------------------------------
class TestParityComparison:
    def test_identical_streams_match(self):
        result = compare(["a", "b", "c"], ["a", "b", "c"])
        assert result["match"] is True
        assert result["first_divergence"] is None

    def test_first_divergence_is_reported_in_context(self):
        result = compare(["a", "b", "c"], ["a", "x", "c"])
        assert result["match"] is False
        window = result["first_divergence"]
        assert window["index"] == 1
        assert "x" in window["actual"]

    def test_missing_tail_counts_as_divergence(self):
        result = compare(["a", "b"], ["a", "b", "c"])
        assert result["match"] is False
        assert result["first_divergence"]["index"] == 2

    def test_normalise_ignores_volatile_fields(self):
        rows = [
            {"kind": "signal", "ts": "2024-01-01T00:00:00Z",
             "payload": {"symbol": "BTC/USDT", "strategy": "S1", "side": "buy", "reason": "x",
                         "signal_id": "abc123"}},
            {"kind": "signal", "ts": "2024-01-01T00:00:00Z",
             "payload": {"symbol": "BTC/USDT", "strategy": "S1", "side": "buy", "reason": "x",
                         "signal_id": "different-id"}},
        ]
        assert normalise(rows, ("signal",))[0] == normalise(rows, ("signal",))[1]


@pytest.mark.asyncio
async def test_end_to_end_parity_on_a_recorded_tape(cfg):
    """The real thing: record ticks and books, replay them, require identical decisions.

    Kept to a short window so the suite stays fast; the CLI runs the same code over a
    full session.
    """
    result = await run_parity(
        cfg, start=datetime(2024, 1, 1, tzinfo=UTC), minutes=1200, seed=7
    )
    assert result["mode"] == "tape"
    assert result["passed"] is True, result["decisions"]["first_divergence"]
    # the run must actually have decided something, or "match" would be vacuous
    assert result["decisions"]["expected_count"] > 0
    assert result["fills"]["informational"]["expected_count"] > 0
    assert result["tape"]["ticks"] > 0 and result["tape"]["books"] > 0

"""The bounded research loop: every attempt counted, the winner deflated, nothing promoted."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tradebot.backtest.metrics import _daily_returns
from tradebot.config import load_config
from tradebot.research import (
    ResearchReport,
    candidates,
    format_report,
    search,
    sharpe_of,
    space_from_config,
)

START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(days=4)


def curve(per_day_return: float, *, days: int = 4, points_per_day: int = 24) -> list[tuple[datetime, Decimal]]:
    """A deterministic equity curve with a constant drift."""
    out: list[tuple[datetime, Decimal]] = []
    equity = 1000.0
    for day in range(days):
        for point in range(points_per_day):
            equity *= 1.0 + per_day_return / points_per_day
            out.append((START + timedelta(days=day, hours=point), Decimal(str(round(equity, 6)))))
    return out


class FakeResult:
    def __init__(self, net_return: float, trades: int = 5, curve_points: Any = None) -> None:
        self.net_return = Decimal(str(net_return))
        self.trades = [object()] * trades
        self.curve = curve_points if curve_points is not None else curve(net_return / 4)


def researcher_by_param(values: dict[str, float], *, fail: set[str] | None = None) -> Any:
    """A fake engine: the candidate's ``a`` value decides the return."""

    async def runner(params: dict[str, Any], cfg: Any, start: datetime, end: datetime) -> Any:
        key = str(params.get("a"))
        if fail and key in fail:
            raise RuntimeError("this candidate blew up")
        days = (end - start).total_seconds() / 86_400
        return FakeResult(values[key] * days / 4, curve_points=curve(values[key] / 4))

    return runner


def run_search(cfg: Any, **kwargs: Any) -> ResearchReport:
    return asyncio.run(search(cfg, **kwargs))


class TestSpace:
    def test_candidates_are_the_cartesian_product(self):
        assert candidates({"a": [1, 2], "b": [3]}) == [{"a": 1, "b": 3}, {"a": 2, "b": 3}]

    def test_an_empty_space_is_one_default_candidate(self):
        assert candidates({}) == [{}]

    def test_the_built_space_stays_close_to_the_configured_value(self):
        cfg = load_config()
        space = space_from_config(cfg, "S1_ema_trend", steps=3, spread=0.5)
        assert space, "the default strategy has numeric parameters"
        configured = cfg.strategies.strategies["S1_ema_trend"].params
        for name, values in space.items():
            base = configured[name]
            assert min(values) < base < max(values)
            # integer rounding can push a small value a little further than the spread
            assert max(values) <= base * 1.26 + 1 and min(values) >= base * 0.74 - 1

    def test_string_parameters_are_not_searched(self):
        cfg = load_config()
        space = space_from_config(cfg, "S1_ema_trend")
        for values in space.values():
            assert all(isinstance(value, (int, float)) for value in values)

    def test_an_unknown_strategy_is_refused(self):
        with pytest.raises(KeyError):
            space_from_config(load_config(), "S9_nope")


class TestSharpe:
    def test_a_rising_curve_has_a_positive_sharpe(self):
        assert sharpe_of(curve(0.01)) > 0

    def test_a_flat_curve_has_no_sharpe(self):
        flat = [(START + timedelta(hours=i), Decimal("1000")) for i in range(20)]
        assert sharpe_of(flat) == 0.0

    def test_a_short_curve_has_no_sharpe_rather_than_a_wrong_one(self):
        assert sharpe_of([(START, Decimal("1000"))]) == 0.0

    def test_a_falling_curve_has_a_negative_sharpe(self):
        assert sharpe_of(curve(-0.01)) < 0

    def test_daily_returns_are_used_when_there_are_enough_days(self):
        assert len(_daily_returns(curve(0.01, days=5))) == 4


class TestSearch:
    def test_every_candidate_is_evaluated_and_logged(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2, 3, 4], "b": [10, 20]},
            start=START,
            end=END,
            stages=(0.5, 1.0),
            seed=1,
            researcher=researcher_by_param({"1": 0.01, "2": 0.02, "3": -0.01, "4": 0.0}),
        )
        assert report.stages[0]["evaluated"] == 8
        assert len([t for t in report.trials if t.stage == 0]) == 8
        assert report.multiple_testing["attempts_logged"] == len(report.trials)

    def test_halving_keeps_the_better_half(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2, 3, 4]},
            start=START,
            end=END,
            stages=(0.5, 1.0),
            keep=0.5,
            seed=1,
            researcher=researcher_by_param({"1": 0.01, "2": 0.04, "3": -0.03, "4": 0.02}),
        )
        assert report.stages[0]["advanced"] == 2
        advanced = {t.params["a"] for t in report.trials if t.survived and t.stage == 0}
        assert advanced == {2, 4}, "the two best candidates should be the ones that advanced"

    def test_the_winner_is_the_best_final_stage_candidate(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2, 3]},
            start=START,
            end=END,
            stages=(0.5, 1.0),
            seed=3,
            researcher=researcher_by_param({"1": -0.02, "2": 0.03, "3": 0.01}),
        )
        assert report.best is not None
        assert report.best.params["a"] == 2

    def test_a_crashing_candidate_is_a_logged_trial_not_a_crash(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2]},
            start=START,
            end=END,
            stages=(1.0,),
            seed=1,
            researcher=researcher_by_param({"1": 0.02, "2": 0.02}, fail={"2"}),
        )
        failed = [trial for trial in report.trials if trial.error]
        assert len(failed) == 1 and "blew up" in (failed[0].error or "")
        assert any(trial.params["a"] == 2 for trial in report.trials), "a failure still counts"

    def test_the_budget_caps_the_number_of_candidates(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2, 3, 4, 5, 6]},
            start=START,
            end=END,
            stages=(1.0,),
            budget=3,
            seed=1,
            researcher=researcher_by_param({str(i): 0.01 for i in range(1, 7)}),
        )
        assert len([t for t in report.trials if t.stage == 0]) == 3

    def test_the_seed_makes_the_order_reproducible(self):
        kwargs: dict[str, Any] = {
            "strategy": "S1_ema_trend",
            "space": {"a": [1, 2, 3, 4, 5]},
            "start": START,
            "end": END,
            "stages": (0.5, 1.0),
            "seed": 42,
            "researcher": researcher_by_param({str(i): 0.01 * i for i in range(1, 6)}),
        }
        first = run_search(load_config(), **kwargs)
        second = run_search(load_config(), **kwargs)
        assert [t.params for t in first.trials] == [t.params for t in second.trials]

    def test_an_unknown_strategy_is_refused_before_any_work(self):
        with pytest.raises(KeyError):
            run_search(
                load_config(),
                strategy="S9_nope",
                space={"a": [1]},
                start=START,
                end=END,
                researcher=researcher_by_param({"1": 0.0}),
            )

    def test_an_empty_space_is_refused(self):
        with pytest.raises(ValueError, match="empty"):
            run_search(
                load_config(),
                strategy="S1_ema_trend",
                space={},
                start=START,
                end=END,
                researcher=researcher_by_param({"1": 0.0}),
            )


class TestVerdicts:
    def test_losing_everywhere_recommends_nothing(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2]},
            start=START,
            end=END,
            stages=(1.0,),
            seed=1,
            researcher=researcher_by_param({"1": -0.02, "2": -0.01}),
        )
        assert report.recommend is False
        assert "no edge" in report.verdict

    def test_a_winner_that_luck_explains_is_not_recommended(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2, 3, 4, 5, 6, 7, 8]},
            start=START,
            end=END,
            stages=(1.0,),
            seed=1,
            researcher=researcher_by_param({str(i): 0.002 for i in range(1, 9)}),
        )
        # every candidate identical: the best is exactly what luck would produce
        assert report.recommend is False
        assert "luck" in report.verdict or "weak" in report.verdict

    def test_a_strong_winner_is_recommended_only_for_re_testing(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1]},
            start=START,
            end=END,
            stages=(1.0,),
            seed=1,
            researcher=researcher_by_param({"1": 0.05}),
        )
        assert report.recommend is True
        assert "not a promotion" in report.verdict
        assert any("walk-forward" in step for step in report.next_steps)

    def test_the_report_says_what_would_have_to_happen_next(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1]},
            start=START,
            end=END,
            stages=(1.0,),
            researcher=researcher_by_param({"1": 0.01}),
        )
        joined = " ".join(report.next_steps)
        assert "walk-forward" in joined and "robustness" in joined and "gates" in joined

    def test_the_report_serialises(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2]},
            start=START,
            end=END,
            stages=(1.0,),
            researcher=researcher_by_param({"1": 0.01, "2": -0.01}),
        )
        payload = report.as_dict()
        assert payload["attempts"] == 2
        assert payload["space_size"] == 2
        assert payload["verdict"]

    def test_the_terminal_report_leads_with_the_count(self):
        report = run_search(
            load_config(),
            strategy="S1_ema_trend",
            space={"a": [1, 2]},
            start=START,
            end=END,
            stages=(1.0,),
            researcher=researcher_by_param({"1": 0.01, "2": -0.01}),
        )
        text = format_report(report)
        assert text.splitlines()[0].startswith("search:")
        assert "attempts actually run" in text
        assert "recommendation:" in text

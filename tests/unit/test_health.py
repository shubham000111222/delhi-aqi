"""The health model, and the two rules that keep it from being decoration.

The states themselves are trivial; what needs pinning is that a component cannot be
healthy while failing a check, that flapping is measured over a window rather than a
counter, and that a restart budget actually refuses the fourth restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradebot.health import Check, HealthModel, RestartBudget, State

T0 = datetime(2026, 11, 4, tzinfo=UTC)


class TestStates:
    def test_a_component_with_passing_checks_is_healthy(self):
        model = HealthModel()
        component = model.report("feed", now=T0, ticks=Check())
        assert component.state is State.HEALTHY
        assert component.score == 1.0
        assert component.last_ok == T0

    def test_liveness_failure_is_failed(self):
        model = HealthModel()
        assert model.report("feed", now=T0, ticks=Check(liveness=False)).state is State.FAILED

    def test_readiness_or_correctness_failure_is_degraded(self):
        model = HealthModel()
        assert model.report("feed", now=T0, ticks=Check(readiness=False)).state is State.DEGRADED
        assert model.report("feed", now=T0, ticks=Check(correctness=False)).state is State.DEGRADED

    def test_liveness_outranks_readiness(self):
        """A dead component with a ready check is still failed, not degraded."""
        model = HealthModel()
        component = model.report("feed", now=T0, a=Check(liveness=False), b=Check())
        assert component.state is State.FAILED

    def test_the_score_is_the_share_of_passing_checks(self):
        model = HealthModel()
        component = model.report(
            "store", now=T0, write=Check(), disk=Check(readiness=False), wal=Check()
        )
        assert component.state is State.DEGRADED
        assert round(component.score, 3) == 0.667

    def test_an_empty_report_is_not_a_pass(self):
        component = HealthModel().register("feed")
        assert component.score == 1.0  # nothing has gone wrong yet
        component.state = State.FAILED
        assert component.score == 0.0  # a failed component never scores clean


class TestTransitionsAndFlapping:
    def test_only_changes_are_recorded(self):
        model = HealthModel()
        for _ in range(4):
            model.report("feed", now=T0, ticks=Check())
        assert model.changes == []

        model.report("feed", now=T0, ticks=Check(readiness=False))
        assert len(model.changes) == 1
        change = model.changes[0]
        assert change["component"] == "feed"
        assert (change["from"], change["to"]) == ("HEALTHY", "DEGRADED")

    def test_five_changes_in_ten_minutes_is_not_flapping_but_six_is(self):
        model = HealthModel()
        # five transitions: HEALTHY -> DEGRADED -> HEALTHY -> ... starting at minute 1
        for step in range(1, 6):
            model.report(
                "feed",
                now=T0 + timedelta(minutes=step),
                ticks=Check(readiness=step % 2 == 0),
            )
        assert len(model.components["feed"].transitions) == 5
        assert model.flapping(now=T0 + timedelta(minutes=6)) == []

        model.report("feed", now=T0 + timedelta(minutes=6), ticks=Check(readiness=True))
        assert len(model.components["feed"].transitions) == 6
        assert model.flapping(now=T0 + timedelta(minutes=6)) == ["feed"]

    def test_changes_outside_the_window_do_not_count(self):
        model = HealthModel()
        for step in range(1, 8):
            model.report("feed", now=T0 + timedelta(minutes=step), ticks=Check(readiness=step % 2 == 0))
        assert len(model.components["feed"].transitions) >= 6
        assert model.flapping(now=T0 + timedelta(hours=2)) == []


class TestSummary:
    def test_the_overall_state_is_the_worst_critical_component(self):
        model = HealthModel()
        model.report("feed", now=T0, ticks=Check())
        model.report("store", now=T0, write=Check(readiness=False))
        model.register("alerting", critical=False).state = State.FAILED

        assert model.worst() is State.DEGRADED
        snapshot = model.snapshot(now=T0)
        assert snapshot["overall"] == "DEGRADED"
        assert snapshot["components"]["alerting"]["state"] == "FAILED"
        assert snapshot["components"]["store"]["critical"] is True

    def test_the_overall_score_is_the_worst_critical_score(self):
        model = HealthModel()
        model.report("feed", now=T0, ticks=Check())
        model.report("store", now=T0, write=Check(), disk=Check(readiness=False))
        assert model.snapshot()["score"] == round(1 / 2, 3)

    def test_the_snapshot_is_json_serialisable(self):
        import json

        model = HealthModel()
        model.report("feed", now=T0, ticks=Check(readiness=False, detail="stale 2 symbols"))
        payload = json.dumps(model.snapshot(now=T0))  # must not raise
        assert "feed" in payload and "DEGRADED" in payload

    def test_an_optional_component_cannot_hold_the_system(self):
        model = HealthModel()
        model.report("feed", now=T0, ticks=Check())
        model.register("ai", critical=False)
        model.report("ai", now=T0, model=Check(liveness=False))
        assert model.worst() is State.HEALTHY


class TestRestartBudget:
    def test_three_restarts_are_allowed_and_the_fourth_is_refused(self):
        budget = RestartBudget(max_per_hour=3)
        for _ in range(3):
            assert budget.allow("feed", now=T0) is True
            budget.record("feed", now=T0)
        assert budget.allow("feed", now=T0) is False
        assert budget.used("feed", now=T0) == 3

    def test_the_window_rolls_forward(self):
        budget = RestartBudget(max_per_hour=3)
        for _ in range(3):
            budget.record("feed", now=T0)
        assert budget.allow("feed", now=T0 + timedelta(minutes=59)) is False
        assert budget.allow("feed", now=T0 + timedelta(hours=1, minutes=1)) is True

    def test_budgets_are_per_component(self):
        budget = RestartBudget(max_per_hour=1)
        budget.record("feed", now=T0)
        assert budget.allow("feed", now=T0) is False
        assert budget.allow("store", now=T0) is True

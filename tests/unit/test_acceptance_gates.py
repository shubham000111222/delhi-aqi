"""Promotion must fail when evidence is missing or thresholds are merely equal."""
from tradebot.backtest.metrics import GateResult, Metrics, acceptance_gates, all_gates_passed
from tradebot.core.money import D


def test_missing_cost_stress_and_oos_evidence_cannot_promote():
    metrics = Metrics(profit_factor=D("2"), sharpe=D("2"), max_drawdown=D("-0.10"), trade_count=250)
    gates = acceptance_gates(metrics, mc={"p95_drawdown": -0.1})
    by_name = {g.name: g for g in gates}
    assert not by_name["cost_stress_2x"].passed
    assert not by_name["out_of_sample_provenance"].passed
    assert not all_gates_passed(gates)
    assert not all_gates_passed([])
    assert not all_gates_passed([GateResult("sharpe", True, "incomplete")])


def test_strict_gate_boundaries_and_missing_monte_carlo():
    metrics = Metrics(profit_factor=D("1.3"), sharpe=D("1.0"), max_drawdown=D("-0.15"), trade_count=200)
    gates = {g.name: g for g in acceptance_gates(metrics)}
    for name in ("profit_factor", "sharpe", "max_drawdown", "monte_carlo_drawdown"):
        assert not gates[name].passed
    assert gates["trade_count"].passed
    stress = Metrics(total_return=D("0.01"))
    gates = {g.name: g for g in acceptance_gates(metrics, mc={"p95_drawdown": -0.20}, cost_stress_metrics=stress)}
    assert not gates["monte_carlo_drawdown"].passed
    assert gates["cost_stress_2x"].passed

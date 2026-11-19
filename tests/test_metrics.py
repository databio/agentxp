import math

import pytest

from agentxp import metrics


def test_cost_units_weights():
    t = {"input": 100, "cache_write": 100, "cache_read": 1000, "output": 10}
    assert metrics.cost_units(t) == 100 + 125 + 100 + 50


def test_failures_are_charged_to_successes():
    runs = [{"passed": True, "cost_units": 100}, {"passed": False, "cost_units": 300}]
    assert metrics.task_cps(runs) == 200 / 0.5  # mean over ALL runs / success rate
    assert math.isinf(metrics.task_cps([{"passed": False, "cost_units": 5}]))


def row(task, passed, cu, shape_only=False):
    return {"task": task, "run": 0, "passed": passed, "outcome_passed": passed or shape_only,
            "cost_units": cu, "calls": 1, "turns": 2, "wrong_tools": [], "cost_usd": 0.01,
            "judge_cost_usd": 0, "tokens": {"input": 1, "cache_write": 0, "cache_read": 0, "output": 1}}


def summary(rates_costs, gate_ok=True, **meta):
    rows = []
    for task, runs in rates_costs.items():
        rows += [row(task, p, c) for p, c in runs]
    rows.append(row("g", gate_ok, 1))
    w = {t: 1 / len(rates_costs) for t in rates_costs} | {"g": 0.0}
    return metrics.summarize(rows, w, {"g"}, {"driver": "claude", "model": "m", "harness": "0.1.0",
                                             "tasks_hash": "h", "outcome_hash": "o", **meta})


def test_summarize():
    s = summary({"a": [(True, 100), (False, 100)], "b": [(True, 50)] * 2})
    assert s["weighted_success"] == 0.75
    assert s["cost_per_success"] == pytest.approx(0.5 * 200 + 0.5 * 50)
    assert s["gates_passed"] and s["tasks"]["g"]["weight"] == 0
    assert s["runs"] == 5 and s["cost_usd"] == 0.05


OLD = {"a": [(True, 1000 + i) for i in range(5)], "b": [(True, 2000 - i) for i in range(5)]}


def test_cost_change_must_beat_the_old():
    cheaper = {"a": [(True, 700 + i) for i in range(5)], "b": [(True, 1400 - i) for i in range(5)]}
    res = metrics.compare(summary(OLD), summary(cheaper), change="cost")
    assert res["ship"] and res["interval"][1] < 0.95 and res["ratio"] == pytest.approx(0.7, abs=0.01)
    same = metrics.compare(summary(OLD), summary(OLD), change="cost")
    assert not same["ship"]  # equal is not a win


def test_cost_win_needs_the_margin():
    small = {k: [(p, c * 0.97) for p, c in v] for k, v in OLD.items()}  # 3% cheaper, no noise
    res = metrics.compare(summary(OLD), summary(small), change="cost")
    assert res["interval"][1] < 1.0 and not res["ship"]


def test_dearer_but_passing_is_noted():
    up3 = {k: [(p, c * 1.03) for p, c in v] for k, v in OLD.items()}
    res = metrics.compare(summary(OLD), summary(up3))
    assert res["ship"] and res["notes"]


def test_cost_win_needs_five_runs():
    old = {"a": [(True, 1000)] * 3}
    new = {"a": [(True, 500)] * 3}
    res = metrics.compare(summary(old), summary(new), change="cost")
    assert not res["ship"] and any("too few runs" in r for r in res["reasons"])


def test_noisy_gain_does_not_count_as_a_cost_win():
    old = {"a": [(True, c) for c in (200, 2000, 300, 1800, 900)]}
    new = {"a": [(True, c) for c in (150, 1900, 280, 1700, 800)]}  # 7% cheaper, very noisy
    res = metrics.compare(summary(old), summary(new), change="cost")
    assert res["ratio"] < 1 and res["interval"][1] >= 1 and not res["ship"]


def test_other_change_allows_five_percent():
    up4 = {k: [(p, c * 1.04) for p, c in v] for k, v in OLD.items()}
    up8 = {k: [(p, c * 1.08) for p, c in v] for k, v in OLD.items()}
    assert metrics.compare(summary(OLD), summary(up4))["ship"]
    assert not metrics.compare(summary(OLD), summary(up8))["ship"]


def test_success_and_gate_rules():
    worse = {"a": [(True, 1000)] * 2 + [(False, 1000)] * 3, "b": OLD["b"]}
    res = metrics.compare(summary(OLD), summary(worse))
    assert not res["ship"] and any("mostly passing" in r for r in res["reasons"])
    assert any("weighted success fell" in r for r in res["reasons"])
    res = metrics.compare(summary(OLD), summary(OLD, gate_ok=False))
    assert any("gate g" in r for r in res["reasons"])


def test_bootstrap_is_seeded_and_brackets_the_point():
    old, new = summary(OLD), summary({k: [(p, c * 0.9) for p, c in v] for k, v in OLD.items()})
    a = metrics.bootstrap_ratio(old, new, ["a", "b"], "passed", 500, seed=1)
    b = metrics.bootstrap_ratio(old, new, ["a", "b"], "passed", 500, seed=1)
    assert a == b and a[1] <= a[0] <= a[2]


def test_outcome_only_uses_outcome_passed():
    rows_old = [row("a", True, 100)]
    rows_new = [row("a", False, 100, shape_only=True)]  # failed only a call-shape check
    w, meta = {"a": 1.0}, {"driver": "c", "model": "m", "harness": "0.1.0", "tasks_hash": "x", "outcome_hash": "o"}
    old = metrics.summarize(rows_old, w, set(), meta)
    new = metrics.summarize(rows_new, w, set(), {**meta, "tasks_hash": "y"})
    assert not metrics.compare(old, new)["ship"]
    assert metrics.compare(old, new, outcome_only=True)["ship"]
    assert metrics.comparable(old, new, False) and not metrics.comparable(old, new, True)


def test_comparable_refuses_different_model():
    assert any("model" in p for p in metrics.comparable(summary(OLD), summary(OLD, model="x"), False))

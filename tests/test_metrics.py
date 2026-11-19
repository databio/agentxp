import math
import random

import pytest

from agentxp import metrics


def test_cost_units_weights():
    t = {"input": 100, "cache_write": 100, "cache_read": 1000, "output": 10}
    assert metrics.cost_units(t) == 100 + 125 + 100 + 50


def test_failures_are_charged_to_successes():
    runs = [{"passed": True, "cost_units": 100}, {"passed": False, "cost_units": 300}]
    assert metrics.task_cps(runs) == 200 / 0.5  # mean over ALL runs / success rate
    dead = [{"passed": False, "cost_units": 5}] * 5
    assert metrics.task_cps(dead) == 5 / 0.1  # success rate floored at 1/(2n): dear but finite


def test_dead_task_fixed_can_ship_and_both_dead_is_finite():
    dead = {"a": [(False, 1000)] * 5, "b": OLD["b"]}
    fixed = {"a": [(True, 1000)] * 5, "b": OLD["b"]}
    res = metrics.compare(summary(dead), summary(fixed))
    assert res["ship"] and res["weighted_success"]["new"] > res["weighted_success"]["old"]
    assert res["cps_ratio"] < 1
    both = metrics.compare(summary(dead), summary(dead))
    assert both["ship"] and both["ratio"] == pytest.approx(1.0)
    assert all(math.isfinite(x) for x in [both["ratio"], *both["interval"], both["cps_ratio"]])
    assert summary(dead)["cost_per_success"] is not None
    cost = metrics.compare(summary(dead), summary(fixed), change="cost")
    assert cost["ship"] and cost["interval"][1] < 0.95  # fixing a dead task is a real cost win


def test_partial_runs_do_not_ship():
    only_a = {"a": OLD["a"]}
    res = metrics.compare(summary(OLD), summary(only_a))
    assert not res["ship"] and res["missing"] == ["b"] and any("one side" in r for r in res["reasons"])
    res = metrics.compare(summary(OLD), summary(OLD, partial="--only"))
    assert not res["ship"] and any("partial (--only)" in r for r in res["reasons"])
    budget = summary(OLD)
    budget["skipped"] = ["b"]
    assert any("budget" in r for r in metrics.compare(summary(OLD), budget)["reasons"])


def test_free_failures_are_charged_the_task_mean():
    rows = [row("a", True, 100), row("a", True, 300), row("a", False, 0)]  # a timeout with no usage
    s = metrics.summarize(rows, {"a": 1.0}, set(), {})
    assert [r["cost_units"] for r in s["tasks"]["a"]["runs"]] == [100, 300, 200]
    assert s["tasks"]["a"]["runs"][2]["cost_charged"]
    assert metrics.summarize([row("a", True, 0)], {"a": 1.0}, set(), {})["tasks"]["a"]["mean_cost_units"] == 0


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
    assert res["ship"] and "measurably dearer" in res["notes"][0]


def test_dearer_within_noise_passes_with_a_note():
    old = {"a": [(True, c) for c in (200, 2000, 300, 1800, 900)]}
    new = {"a": [(True, c * 1.10) for c in (200, 2000, 300, 1800, 900)][::-1][:4] + [(True, 100)]}
    res = metrics.compare(summary(old), summary(new))
    assert res["ratio"] > 1.05 and res["interval"][0] <= 1.0
    assert res["ship"] and "within noise" in res["notes"][0]


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
    res = metrics.compare(summary(OLD), summary(up8))
    assert not res["ship"] and res["measure"] == "cost per run"


def test_one_flaky_run_does_not_trip_the_cost_rule():
    base = {"a": OLD["a"], "b": OLD["b"], "c": OLD["a"], "d": OLD["b"], "e": OLD["a"]}
    flaky = {**base, "a": OLD["a"][:4] + [(False, 1000)]}  # one failed run, same tokens
    res = metrics.compare(summary(base), summary(flaky))
    assert res["ship"] and res["cps_ratio"] > 1.03  # cost per success rose; cost per run did not


def test_success_and_gate_rules():
    worse = {"a": [(True, 1000)] * 2 + [(False, 1000)] * 3, "b": OLD["b"]}
    res = metrics.compare(summary(OLD), summary(worse))
    assert not res["ship"] and any("mostly passing" in r for r in res["reasons"])
    assert any("weighted success fell" in r for r in res["reasons"])
    slight = {k: v * 4 for k, v in OLD.items()}  # 20 runs per task
    slight["a"] = slight["a"][:19] + [(False, 1000)]  # success 1.0 -> 0.975: within 0.05
    res = metrics.compare(summary({k: v * 4 for k, v in OLD.items()}), summary(slight))
    assert not any("weighted success" in r for r in res["reasons"])
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


def test_aa_resamples_of_a_noisy_baseline_mostly_ship():
    """An A/A compare: new is the baseline's own runs resampled. It must pass the large majority."""
    rng = random.Random(3)
    base = {f"t{i}": [(True, rng.uniform(5000, 15000)) for _ in range(5)] for i in range(12)}
    base["t0"][0] = base["t1"][0] = (False, 9000)  # two tasks pass 4 of 5, like the real baselines
    old = summary(base)
    ships = cost_ok = 0
    for k in range(100):
        res = metrics.compare(old, summary({t: rng.choices(runs, k=5) for t, runs in base.items()}),
                              resamples=300, seed=k)
        ships += res["ship"]
        cost_ok += not any(r.startswith(("cost", "weighted")) for r in res["reasons"])
    assert ships >= 85 and cost_ok >= 95  # the rest: a 4-of-5 task resampled to 2 of 5

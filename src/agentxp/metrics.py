"""Cost units, the run summary, and the ship rule.

``cost_units = input + 1.25 * cache_write + 0.1 * cache_read + 5 * output``, with
``input`` the uncached input tokens. It weighs tokens the way they are billed,
so a change that only moves tokens into the cache does not look like a win.

Cost per successful task: per task, mean cost units over ALL runs divided by its
success rate (failures are charged to the successes); then the weighted sum. The
success rate is floored at 1/(2n) for n runs, so a task that never passed is very
dear (2n times its mean cost) but finite, and a fix for it can still ship.
"""

from __future__ import annotations

import math
import random

MIN_SHIP_RUNS = 5  # a floor on interval quality for cost claims
#: A cost claim must beat this. An A/A run (same code twice, two sessions) came out 3.2%
#: apart with a tight interval: the bootstrap sees within-session noise only, so the
#: margin covers between-session shifts. Widen it to ~2x the largest A/A shift you see.
COST_MARGIN = 0.95
#: Weighted success may fall by at most this much (one flaky run in a big suite must not block).
SUCCESS_SLACK = 0.05
#: Claude's list-price ratios. For codex they are approximate: fine against a codex baseline only.
COST_WEIGHTS = {"input": 1.0, "cache_write": 1.25, "cache_read": 0.1, "output": 5.0}


def cost_units(tokens: dict) -> float:
    return round(sum(w * float(tokens.get(k) or 0) for k, w in COST_WEIGHTS.items()), 1)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def task_cps(runs: list[dict], key: str = "passed") -> float:
    """Cost per success for one task's runs; the success rate is floored at 1/(2n)."""
    if not runs:
        return 0.0
    rate = max(_mean([1.0 if r[key] else 0.0 for r in runs]), 1 / (2 * len(runs)))
    return _mean([r["cost_units"] for r in runs]) / rate


def weighted_cps(tasks: dict[str, dict], runs: dict[str, list[dict]], key: str = "passed",
                 per_run: bool = False) -> float:
    """Weighted cost per success (or, with ``per_run``, weighted mean cost per run)."""
    total_w = sum(tasks[t]["weight"] for t in runs) or 1.0
    one = (lambda rs: _mean([r["cost_units"] for r in rs])) if per_run else (lambda rs: task_cps(rs, key))
    return sum(tasks[t]["weight"] / total_w * one(rs) for t, rs in runs.items() if tasks[t]["weight"])


def charge_free_failures(rs: list[dict]) -> list[dict]:
    """A failed run that recorded no tokens (it timed out or crashed before any usage came
    back) is charged the task's mean cost over the runs that did, so it is not free."""
    paid = [r["cost_units"] for r in rs if r["cost_units"]]
    if not paid:
        return rs
    mean = round(_mean(paid), 1)
    return [{**r, "cost_units": mean, "cost_charged": True} if not r["passed"] and not r["cost_units"] else r
            for r in rs]


def summarize(rows: list[dict], weights: dict[str, float], gates: set[str], meta: dict) -> dict:
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("skipped"):
            by_task.setdefault(r["task"], []).append(r)
    by_task = {t: charge_free_failures(rs) for t, rs in by_task.items()}
    tasks = {}
    for tid, rs in by_task.items():
        rate = _mean([1.0 if r["passed"] else 0.0 for r in rs])
        tasks[tid] = {
            "weight": round(weights.get(tid, 0.0), 6),
            "gate": tid in gates,
            "runs": [{"passed": r["passed"], "outcome_passed": r["outcome_passed"], "cost_units": r["cost_units"],
                      **({"cost_charged": True} if r.get("cost_charged") else {})} for r in rs],
            "success_rate": round(rate, 3),
            "mean_cost_units": round(_mean([r["cost_units"] for r in rs]), 1),
            "cost_per_success": round(task_cps(rs), 1),
            "mean_calls": round(_mean([r["calls"] for r in rs]), 2),
            "mean_turns": round(_mean([r["turns"] for r in rs]), 2),
            "wrong_tools": sorted({w for r in rs for w in r["wrong_tools"]}),
        }
    weighted = {t: v for t, v in tasks.items() if not v["gate"]}
    total_w = sum(v["weight"] for v in weighted.values()) or 1.0
    cps = weighted_cps(tasks, {t: by_task[t] for t in weighted})
    done = [r for r in rows if not r.get("skipped")]
    return {
        **{k: meta.get(k) for k in ("harness", "driver", "driver_version", "model", "tasks_hash",
                                    "outcome_hash", "server_commit", "label", "partial")},
        "weighted_success": round(sum(v["weight"] * v["success_rate"] for v in weighted.values()) / total_w, 3),
        "cost_per_success": round(cps, 1),
        "gates_passed": all(v["success_rate"] == 1.0 for v in tasks.values() if v["gate"]),
        "runs": len(done),
        "passed": sum(r["passed"] for r in done),
        "skipped": sorted({r["task"] for r in rows if r.get("skipped")}),
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in done), 4),
        "judge_cost_usd": round(sum(r.get("judge_cost_usd") or 0 for r in done), 4),
        "tokens": {k: sum(r["tokens"][k] for r in done) for k in COST_WEIGHTS},
        "tasks": tasks,
    }


def bootstrap_ratio(old: dict, new: dict, common: list[str], key: str, n: int = 2000,
                    seed: int = 0, per_run: bool = False) -> tuple[float, float, float]:
    """Point estimate and 90% interval of new/old weighted cost per success (or per run),
    resampling each task's runs with replacement on each side."""
    rng = random.Random(seed)
    o_runs = {t: old["tasks"][t]["runs"] for t in common}
    n_runs = {t: new["tasks"][t]["runs"] for t in common}
    w = {t: {"weight": new["tasks"][t]["weight"]} for t in common}

    def ratio(o, nw) -> float:
        a, b = weighted_cps(w, o, key, per_run), weighted_cps(w, nw, key, per_run)
        return b / a if a else (1.0 if not b else math.inf)

    point = ratio(o_runs, n_runs)
    samples = []
    for _ in range(n):
        o = {t: rng.choices(rs, k=len(rs)) for t, rs in o_runs.items()}
        nw = {t: rng.choices(rs, k=len(rs)) for t, rs in n_runs.items()}
        samples.append(ratio(o, nw))
    samples.sort()
    return point, samples[int(0.05 * n)], samples[min(n - 1, int(0.95 * n))]


def compare(old: dict, new: dict, change: str = "other", outcome_only: bool = False,
            resamples: int = 2000, seed: int = 0) -> dict:
    """The ship rule: complete runs, gates, success, then cost. ``change="cost"``: the upper
    end of the 90% interval of new/old cost per success must be below COST_MARGIN. Any other
    change: fails only when new/old cost per run is above 1.05 AND its interval's lower end is
    above 1.00 (dearer by more than 5%, and measurably so). Per run, because a failure is
    already counted by the success rules; per success for a cost claim, so it is not bought
    by failing more."""
    key = "outcome_passed" if outcome_only else "passed"
    reasons: list[str] = []
    common = sorted(set(old["tasks"]) & set(new["tasks"]))
    missing = sorted(set(old["tasks"]) ^ set(new["tasks"]))
    if missing:
        reasons.append(f"tasks on one side only (a partial run?): {', '.join(missing)}")
    for side, s in (("old", old), ("new", new)):
        if s.get("skipped"):
            reasons.append(f"the {side} run skipped runs of {', '.join(s['skipped'])} (budget stop)")
        if s.get("partial"):
            reasons.append(f"the {side} run is partial ({s['partial']}): run the whole task file")
    weighted = [t for t in common if not new["tasks"][t]["gate"] and new["tasks"][t]["weight"]]

    for t, v in new["tasks"].items():
        if v["gate"] and not all(r[key] for r in v["runs"]):
            reasons.append(f"gate {t} failed a run")

    def rate(s: dict, t: str) -> float:
        return _mean([1.0 if r[key] else 0.0 for r in s["tasks"][t]["runs"]])

    total_w = sum(new["tasks"][t]["weight"] for t in weighted) or 1.0
    ws_old = sum(new["tasks"][t]["weight"] * rate(old, t) for t in weighted) / total_w
    ws_new = sum(new["tasks"][t]["weight"] * rate(new, t) for t in weighted) / total_w
    if ws_new < ws_old - SUCCESS_SLACK - 1e-9:
        reasons.append(f"weighted success fell {ws_old:.3f} -> {ws_new:.3f} (more than {SUCCESS_SLACK})")
    for t in weighted:
        if rate(old, t) > 0.5 and rate(new, t) < 0.5:
            reasons.append(f"{t} went from mostly passing to mostly failing")

    per_run = change != "cost"
    point, lo, hi = bootstrap_ratio(old, new, weighted, key, resamples, seed, per_run)
    if change == "cost":
        cost_ok = hi < COST_MARGIN
        rule = f"cost change: the upper end of the 90% interval of new/old must be below {COST_MARGIN:.2f}"
        few = [t for t in weighted if min(len(old["tasks"][t]["runs"]), len(new["tasks"][t]["runs"])) < MIN_SHIP_RUNS]
        if few:
            reasons.append(f"too few runs to claim a cost win (need {MIN_SHIP_RUNS} per task): {', '.join(few)}")
    else:
        cost_ok = not (point > 1.05 and lo > 1.0)
        rule = ("other change: fails when new/old cost per run is above 1.05 and the lower end of its "
                "90% interval is above 1.00")
    notes = []
    if cost_ok and point > 1.05:
        notes.append(f"passes, but is {point - 1:.0%} dearer and within noise; rerun with more runs before trusting it")
    elif cost_ok and lo > 1.0:
        notes.append("passes, but is measurably dearer (the whole interval is above 1.00)")
    measure = "cost per run" if per_run else "cost per success"
    if not cost_ok:
        reasons.append(f"{measure} new/old = {point:.3f} (90% {lo:.3f}-{hi:.3f}); {rule}")
    w = {t: {"weight": new["tasks"][t]["weight"]} for t in weighted}
    o_cps = weighted_cps(w, {t: old["tasks"][t]["runs"] for t in weighted}, key)
    n_cps = weighted_cps(w, {t: new["tasks"][t]["runs"] for t in weighted}, key)
    return {
        "ship": not reasons, "reasons": reasons, "notes": notes, "rule": rule, "measure": measure,
        "ratio": point, "interval": [lo, hi], "cps_ratio": n_cps / o_cps if o_cps else 1.0,
        "weighted_success": {"old": round(ws_old, 3), "new": round(ws_new, 3)},
        "tasks": {t: {"old_rate": rate(old, t), "new_rate": rate(new, t),
                      "old_cps": task_cps(old["tasks"][t]["runs"], key),
                      "new_cps": task_cps(new["tasks"][t]["runs"], key)} for t in weighted},
        "missing": missing,
    }


def comparable(old: dict, new: dict, outcome_only: bool) -> list[str]:
    """Why two summaries cannot be compared (empty when they can)."""
    out = []
    for k in ("driver", "model"):
        if old.get(k) != new.get(k):
            out.append(f"{k} differs: {old.get(k)} vs {new.get(k)}")
    if str(old.get("harness", "")).split(".")[:2] != str(new.get("harness", "")).split(".")[:2]:
        out.append(f"harness version differs: {old.get('harness')} vs {new.get('harness')}")
    h = "outcome_hash" if outcome_only else "tasks_hash"
    if old.get(h) != new.get(h):
        out.append(f"{h} differs (the task file changed)" +
                   ("" if outcome_only else "; --outcome-only compares across renamed tools"))
    return out

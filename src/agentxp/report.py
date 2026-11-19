"""report.md: what happened, per task, and which transcripts to read."""

from __future__ import annotations

import json
from pathlib import Path


def load_dir(d: Path) -> tuple[dict, list[dict], dict]:
    meta = json.loads((d / "meta.json").read_text())
    rows = [json.loads(line) for line in open(d / "runs.jsonl")]
    summary = json.loads((d / "summary.json").read_text())
    return meta, rows, summary


def _num(x, fmt="{:,.0f}") -> str:
    if x is None:
        return "-"
    return "never passed" if x == float("inf") else fmt.format(x)


def render(meta: dict, rows: list[dict], summary: dict, golden: dict[str, dict] | None = None,
           strong: dict | None = None) -> str:
    golden = golden or {}
    out = [
        f"# agentxp: {meta['server']} with {meta['driver']} {meta['model']}",
        "",
        f"Harness {meta['harness']}, {meta['driver']} {meta.get('driver_version')}, server commit "
        f"{meta.get('server_commit')}, judge {meta.get('judge_model') or 'off'}, task file hash "
        f"{meta['tasks_hash']}.",
        "",
        f"- Weighted success: **{summary['weighted_success']:.0%}** ({summary['passed']}/{summary['runs']} runs passed)",
        f"- Gates: **{'all passed' if summary['gates_passed'] else 'FAILED'}**",
        f"- Cost per successful task: **{_num(summary['cost_per_success'])} cost units**",
        f"- Spent: ${summary['cost_usd']:.4f} chatbot + ${summary['judge_cost_usd']:.4f} judge",
    ]
    if meta.get("manual"):
        out.append(f"- Hand-run (manual: true, not run here): {', '.join(meta['manual'])}")
    if summary.get("skipped"):
        out.append(f"- Skipped (budget): {', '.join(summary['skipped'])}")
    out += ["", "| task | weight | passed | calls (golden) | wrong tools | turns | input | cache write | "
            "cache read | output | cost units | $ |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    by_task: dict[str, list[dict]] = {}
    for r in rows:
        if not r.get("skipped"):
            by_task.setdefault(r["task"], []).append(r)
    for tid, rs in by_task.items():
        t = summary["tasks"][tid]
        n = len(rs)

        def avg(f):
            return sum(f(r) for r in rs) / n

        usd = [r["cost_usd"] for r in rs if r.get("cost_usd") is not None]
        weight = "gate" if t["gate"] else f"{t['weight']:.2f}"
        out.append(
            f"| {tid} | {weight} | {sum(r['passed'] for r in rs)}/{n} | {avg(lambda r: r['calls']):.1f} "
            f"({golden.get(tid, {}).get('calls', '-')}) | {', '.join(t['wrong_tools']) or '-'} | "
            f"{avg(lambda r: r['turns']):.1f} | " + " | ".join(
                _num(avg(lambda r, k=k: r['tokens'][k])) for k in ("input", "cache_write", "cache_read", "output"))
            + f" | {_num(t['mean_cost_units'])} | {_num(sum(usd) / len(usd) if usd else None, '{:.4f}')} |")
    failed = [r for r in rows if not r.get("skipped") and not r["passed"]]
    if failed:
        out += ["", "## Failures (read these transcripts)", ""]
        for r in failed:
            msgs = "; ".join(f["msg"] for f in r["failures"])
            out.append(f"- `{r['task']}#{r['run']}` ({r['stop']}): {msgs} -- `transcripts/{r['task']}.{r['run']}.jsonl`")
    judged = [r for r in rows if r.get("judge")]
    if judged:
        out += ["", "## Judge verdicts", ""]
        for r in judged:
            for v in r["judge"]:
                out.append(f"- `{r['task']}#{r['run']}` **{v['verdict']}**: {v['criterion']} "
                           f"(evidence: \"{v['evidence'][:120]}\")")
    if strong is not None:
        out += protocol_table(rows, strong)
    return "\n".join(out) + "\n"


def protocol_table(weak_rows: list[dict], strong: dict) -> list[str]:
    """Weak fails + strong passes: the surface is hard to use. Both fail: something is broken."""
    failed = sorted({r["task"] for r in weak_rows if not r.get("skipped") and not r["passed"]})
    out = ["", f"## Protocol: weak-model failures rerun on {strong.get('model')}", "",
           "| task | strong passed | reading |", "|---|---|---|"]
    for tid in failed:
        t = strong["tasks"].get(tid)
        if t is None:
            out.append(f"| {tid} | not rerun | - |")
            continue
        reading = ("hard to use: fix descriptions or results" if t["success_rate"] >= 0.5
                   else "broken: check the task, fixture or server")
        out.append(f"| {tid} | {t['success_rate']:.0%} | {reading} |")
    return out


def render_compare(old: dict, new: dict, res: dict) -> str:
    lo, hi = res["interval"]
    out = [
        f"# Ship rule: {'SHIP' if res['ship'] else 'DO NOT SHIP'}", "",
        f"{res['rule']}.", "",
        f"- Cost per success new/old: **{res['ratio']:.3f}** (90% interval {lo:.3f} to {hi:.3f})",
        f"- Weighted success: {res['weighted_success']['old']:.3f} -> {res['weighted_success']['new']:.3f}",
    ]
    out += [f"- {r}" for r in res["reasons"]]
    if res["missing"]:
        out.append(f"- Only on one side (ignored): {', '.join(res['missing'])}")
    out += ["", "| task | old success | new success | old cost/success | new cost/success |",
            "|---|---|---|---|---|"]
    for t, v in res["tasks"].items():
        out.append(f"| {t} | {v['old_rate']:.0%} | {v['new_rate']:.0%} | {_num(v['old_cps'])} | {_num(v['new_cps'])} |")
    return "\n".join(out) + "\n"

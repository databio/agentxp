"""agentxp: test a tool surface (an MCP server today) with real chatbots as the users.

    agentxp validate tests/evals/mcp_tasks.yaml          # schema only; safe for CI
    agentxp golden   tests/evals/mcp_tasks.yaml          # shortest paths, no model, free
    agentxp run      tests/evals/mcp_tasks.yaml --driver claude --max-usd 2
    agentxp judge    ~/work/agentxp/<server>/<run>/      # (re)judge rubric lines
    agentxp compare  tests/evals/baseline.json ~/work/agentxp/<server>/<run>/
    agentxp report   ~/work/agentxp/<server>/<run>/ [--strong <rerun dir>]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, metrics, report, runner
from . import spec as specs
from .judge import judge as run_judge
from .judge import rubric
from .spec import CALL_SHAPE


def _load(path: str) -> dict:
    try:
        spec = specs.load(path)
    except specs.SpecError as e:
        raise SystemExit(f"error: {e}")
    errors, warnings = specs.validate(spec)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2)
    return spec


def cmd_validate(args) -> int:
    bad = 0
    for path in args.files:
        try:
            spec = specs.load(path)
        except specs.SpecError as e:
            print(f"{path}: error: {e}")
            bad += 1
            continue
        errors, warnings = specs.validate(spec)
        for w in warnings:
            print(f"{path}: warning: {w}")
        for e in errors:
            print(f"{path}: error: {e}")
        bad += bool(errors)
        if not errors:
            n = len(spec["tasks"])
            gates = sum(bool(t.get("gate")) for t in spec["tasks"])
            print(f"{path}: ok ({n} tasks, {gates} gates, {specs.hashes(spec)['tasks_hash']})")
    return 1 if bad else 0


def cmd_golden(args) -> int:
    spec = _load(args.file)
    root = Path(args.root).resolve() if args.root else runner.repo_root(spec["_path"])
    runner.prepare(spec, root)
    results = runner.run_golden(spec, runner.select(spec, args.only, args.family), root)
    for g in results.values():
        status = "PASS" if g["passed"] else "FAIL"
        path = f"calls={g['calls']} bytes={g['result_bytes']}" if g["has_path"] else "no golden path"
        print(f"{g['task']}: {status} {path} {'; '.join(f['msg'] for f in g['failures'])}")
    return 0 if all(g["passed"] for g in results.values()) else 1


def _write(out: Path, meta: dict, rows: list[dict], summary: dict, golden: dict | None = None) -> None:
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    with open(out / "runs.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    if golden is None and (out / "golden.jsonl").exists():
        golden = {g["task"]: g for g in map(json.loads, open(out / "golden.jsonl"))}
    (out / "report.md").write_text(report.render(meta, rows, summary, golden))


def cmd_run(args) -> int:
    spec = _load(args.file)
    root = Path(args.root).resolve() if args.root else runner.repo_root(spec["_path"])
    runner.prepare(spec, root)
    model = args.model or runner.drivers.get(args.driver).default_model
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "-".join(x for x in (stamp, args.driver, model, args.label) if x)
    out = Path(args.out).expanduser() if args.out else runner.RESULTS / spec["server"]["name"] / name
    rows, meta, summary = runner.run(spec, args, out)
    _write(out, meta, rows, summary)
    print(f"\nweighted success {summary['weighted_success']:.0%}, gates "
          f"{'passed' if summary['gates_passed'] else 'FAILED'}, cost per success "
          f"{summary['cost_per_success']} units, ${summary['cost_usd']:.4f}\nwrote {out}")
    return 0 if summary["passed"] == summary["runs"] and not summary["skipped"] else 1


def cmd_judge(args) -> int:
    out = Path(args.dir).expanduser()
    meta, rows, _ = report.load_dir(out)
    spec = _load(args.tasks or meta["tasks_file"])
    tasks = {t["id"]: t for t in spec["tasks"]}
    meta.update(specs.hashes(spec), judge_model=args.judge_model, tasks_file=spec["_path"],
                rejudged=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    for r in rows:
        task = tasks.get(r["task"])
        if r.get("skipped") or task is None or not rubric(task):
            continue
        hard = [f for f in r["failures"] if f["kind"] != "judge"]
        if hard:
            continue
        calls = [{**c, "text": c.get("result", "")} for c in r["tool_calls"]]
        verdicts, cost = run_judge(task, r.get("prompt", task["prompt"]), calls, r["answer"], args.judge_model)
        soft = [{"kind": "judge", "msg": f"{v['verdict']}: {v['criterion']}"} for v in verdicts
                if v["verdict"] != "yes"]
        r.update(judge=verdicts, failures=soft, passed=not soft, outcome_passed=not soft,
                 judge_cost_usd=(r.get("judge_cost_usd") or 0) + cost)
        print(f"{r['task']}#{r['run']}: {'PASS' if not soft else 'FAIL'} {[v['verdict'] for v in verdicts]}")
    gates = {t["id"] for t in spec["tasks"] if t.get("gate")}
    summary = metrics.summarize(rows, specs.weights(spec), gates, meta)
    _write(out, meta, rows, summary)
    return 0


def _summary(path: str) -> dict:
    p = Path(path).expanduser()
    return json.loads((p / "summary.json" if p.is_dir() else p).read_text())


def cmd_compare(args) -> int:
    old, new = _summary(args.old), _summary(args.new)
    problems = metrics.comparable(old, new, args.outcome_only)
    if problems and not args.force:
        for p in problems:
            print(f"cannot compare: {p}", file=sys.stderr)
        return 2
    res = metrics.compare(old, new, args.change, args.outcome_only, args.resamples, args.seed)
    for p in problems:
        res["reasons"].insert(0, f"(forced) {p}")
    print(report.render_compare(old, new, res))
    return 0 if res["ship"] else 1


def cmd_report(args) -> int:
    d = Path(args.dir).expanduser()
    meta, rows, summary = report.load_dir(d)
    golden = {g["task"]: g for g in map(json.loads, open(d / "golden.jsonl"))} if (d / "golden.jsonl").exists() else {}
    strong = _summary(args.strong) if args.strong else None
    text = report.render(meta, rows, summary, golden, strong)
    (d / "report.md").write_text(text)
    print(text)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agentxp", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--version", action="version", version=f"agentxp {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="check task files (no network, no model)")
    v.add_argument("files", nargs="+")
    v.set_defaults(fn=cmd_validate)

    def pick(sp):
        sp.add_argument("file", help="the task file, e.g. tests/evals/mcp_tasks.yaml")
        sp.add_argument("--only", nargs="*", help="task ids")
        sp.add_argument("--family", nargs="*", help="family ids")
        sp.add_argument("--root", help="folder the server commands run in (default: the one holding tests/)")

    g = sub.add_parser("golden", help="run each golden path with no model and check it")
    pick(g)
    g.set_defaults(fn=cmd_golden)

    r = sub.add_parser("run", help="run the tasks through a chatbot")
    pick(r)
    r.add_argument("--driver", choices=["claude", "codex"], default="claude")
    r.add_argument("--model", help="default: claude-haiku-5-5 (claude), gpt-5.6-luna (codex)")
    r.add_argument("--runs", type=int, help="repeats per task (overrides defaults and doubling)")
    r.add_argument("--jobs", type=int, default=4, help="parallel runs (default 4)")
    r.add_argument("--max-usd", type=float, default=5.0, help="stop starting runs past this spend (default 5)")
    r.add_argument("--max-usd-per-run", type=float, help="claude's per-run dollar cap")
    r.add_argument("--max-turns", type=int)
    r.add_argument("--timeout", type=float, help="seconds per run")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--judge-model", default="claude-opus-5-5")
    r.add_argument("--no-judge", action="store_true")
    r.add_argument("--rerun-failed", metavar="DIR", help="only tasks that failed in this results folder")
    r.add_argument("--label", default="")
    r.add_argument("--out", help="results folder (default ~/work/agentxp/<server>/<stamp>-...)")
    r.set_defaults(fn=cmd_run)

    j = sub.add_parser("judge", help="(re)judge rubric lines in a results folder")
    j.add_argument("dir")
    j.add_argument("--tasks", help="task file (default: the one the run used)")
    j.add_argument("--judge-model", default="claude-opus-5-5")
    j.set_defaults(fn=cmd_judge)

    c = sub.add_parser("compare", help="the ship rule: old (baseline) vs new")
    c.add_argument("old", help="baseline.json, a summary.json, or a results folder")
    c.add_argument("new")
    c.add_argument("--change", choices=["cost", "other"], default="other",
                   help="cost: must beat the old; other (default): must not cost >5%% more")
    c.add_argument("--outcome-only", action="store_true", help="ignore call-shape checks " + str(sorted(CALL_SHAPE)))
    c.add_argument("--force", action="store_true", help="compare even if driver/model/task file differ")
    c.add_argument("--resamples", type=int, default=2000)
    c.add_argument("--seed", type=int, default=0)
    c.set_defaults(fn=cmd_compare)

    rp = sub.add_parser("report", help="write report.md for a results folder")
    rp.add_argument("dir")
    rp.add_argument("--strong", help="results of rerunning the failures on a strong model")
    rp.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

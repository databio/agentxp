"""golden and run: drive the task file against fixtures and chatbots, write results."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, drivers, metrics
from . import judge as judging
from . import spec as specs
from .checks import run_checks
from .spec import CALL_SHAPE, checks_of, fill, is_fresh
from .surfaces.mcp import RESULT_KEEP, Fixture

RESULTS = Path.home() / "work" / "agentxp"


def repo_root(task_file: str) -> Path:
    """Commands run from the folder holding ``tests/`` (or the task file's folder)."""
    p = Path(task_file).resolve().parent
    for d in (p, *p.parents):
        if d.name == "tests":
            return d.parent
    return p


def prepare(spec: dict, root: Path) -> None:
    """Fill ``{python}`` (this harness's interpreter, which has the MCP SDK) and make
    stdio paths absolute, so a chatbot started elsewhere finds them."""
    s = spec["server"]
    for k in ("start", "vars_cmd", "reset"):
        if isinstance(s.get(k), str):
            s[k] = s[k].replace("{python}", sys.executable)
    if s.get("stdio"):
        out = []
        for x in s["stdio"]:
            x = str(x).replace("{python}", sys.executable)
            out.append(str(root / x) if (root / x).is_file() else x)
        s["stdio"] = out


def server_commit(root: Path) -> str | None:
    try:
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError:
        return None
    return (sha + ("+dirty" if dirty else "")) if sha else None


def select(spec: dict, only: list[str] | None, family: list[str] | None) -> list[dict]:
    tasks = [t for t in spec["tasks"] if not t.get("manual")]
    if only:
        tasks = [t for t in tasks if t["id"] in only]
    if family:
        tasks = [t for t in tasks if t.get("family") in family]
    return tasks


class Fixtures:
    """One shared fixture for read tasks; a fresh one for each writing task."""

    def __init__(self, spec: dict, root: Path, log_dir: Path | None):
        self.spec, self.root, self.log_dir = spec, root, log_dir
        self.shared: Fixture | None = None
        self.lock = threading.Lock()
        self.started: list[Fixture] = []

    def get(self, task: dict) -> tuple[Fixture, bool]:
        if self.spec["server"].get("url"):
            with self.lock:
                fx = self.shared or Fixture(self.spec, self.root, self.log_dir).start()
                self.shared = fx
            if is_fresh(task):
                fx.reset()
            return fx, False
        if is_fresh(task):
            return Fixture(self.spec, self.root, self.log_dir).start(), True
        with self.lock:
            if self.shared is None:
                self.shared = Fixture(self.spec, self.root, self.log_dir).start()
                self.started.append(self.shared)
        return self.shared, False

    def close(self) -> None:
        for fx in self.started:
            fx.stop()


def golden_one(task: dict, fixtures: Fixtures) -> dict:
    fx, own = fixtures.get(task)
    try:
        steps = task.get("golden") or []

        def walk():
            outs = []
            for st in steps:
                call = yield st["tool"], fill(st.get("args") or {}, outs, fx.vars)
                outs.append(call["data"])

        calls = fx.call_steps(walk, task.get("as")) if steps else []
        outputs = [c["data"] for c in calls]
        fill([task.get("prompt") or "", *(task.get("replies") or [])], outputs, fx.vars, strict=True)
        check_task = task if steps else {**task, "checks": []}  # no path: nothing to prove
        fails = run_checks(check_task, answer="", calls=calls, golden=outputs, vars=fx.vars,
                           read_state=lambda tool, args: fx.call(tool, args), state_env=_state_env(fx),
                           mode="golden", cwd=fx.root)
        return {"task": task["id"], "passed": not fails, "failures": fails, "calls": len(calls),
                "result_bytes": sum(c["bytes"] for c in calls), "has_path": bool(steps),
                "errors": [c["error_code"] for c in calls if c["error"]], "outputs": outputs,
                "warnings": sorted({f"{c['tool']} returns a text block that differs from its structuredContent; "
                                    "chatbots may see only one of these" for c in calls if split_result(c)}),
                "vars": fx.vars}
    except Exception as e:
        return {"task": task["id"], "passed": False, "failures": [{"kind": "crash", "msg": str(e) or repr(e)}],
                "calls": 0, "result_bytes": 0, "has_path": False, "errors": [], "outputs": [], "warnings": [],
                "vars": {}}
    finally:
        if own:
            fx.stop()


def split_result(call: dict) -> bool:
    """The text block and structuredContent both exist and say different things."""
    data, text = call.get("structured"), call.get("text") or ""
    if data is None or not text.strip():
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = text
    return data not in (parsed, {"result": parsed})


def golden_rows(golden: dict[str, dict]) -> list[str]:
    """golden.jsonl lines: each step's output cut to RESULT_KEEP bytes of JSON."""
    out = []
    for g in golden.values():
        cut = [json.dumps(o, ensure_ascii=False, default=str)[:RESULT_KEEP] for o in g["outputs"]]
        out.append(json.dumps({**g, "outputs": cut}, ensure_ascii=False, default=str))
    return out


def _state_env(fx: Fixture) -> dict:
    env = {"AGENTXP_URL": fx.ready.get("url", ""), "AGENTXP_TOKEN": fx.ready.get("token", "")}
    env.update({f"AGENTXP_VAR_{k.upper()}": str(v) for k, v in fx.vars.items()})
    return env


def run_golden(spec: dict, tasks: list[dict], root: Path, log_dir: Path | None = None) -> dict[str, dict]:
    fixtures = Fixtures(spec, root, log_dir)
    try:
        return {t["id"]: golden_one(t, fixtures) for t in tasks}
    finally:
        fixtures.close()


def runs_for(task: dict, base: int, weight: float) -> int:
    """Repeats: the task's ``runs``; else ``base`` (``--runs`` or ``defaults.runs``),
    doubled for a task holding 15% or more of the weight (it decides the result)."""
    if task.get("runs"):
        return int(task["runs"])
    return base * 2 if weight >= 0.15 else base


def wrong_tools(task: dict, calls: list[dict]) -> list[str]:
    """Calls to tools neither on the golden path nor named by ``called`` / ``order``."""
    expected = {s["tool"] for s in task.get("golden") or []}
    for kind in ("called", "order"):
        for arg in checks_of(task, kind):
            expected |= set(arg)
    if not expected:
        return []
    return list(dict.fromkeys(c["tool"] for c in calls if c["tool"] not in expected))


def run_unit(task: dict, i: int, ctx: dict) -> dict:
    fx, own = ctx["fixtures"].get(task)
    gold = ctx["golden"][task["id"]]
    t0 = time.time()
    try:
        prompt = fill(task["prompt"], gold["outputs"], fx.vars)
        replies = [fill(r, gold["outputs"], fx.vars) for r in task.get("replies") or []]
        transcript = ctx["out"] / "transcripts" / f"{task['id']}.{i}.jsonl"
        ep = ctx["driver"].run(prompt, replies, fx.attachment(task.get("as")), ctx["opts"], transcript)
        fails = []
        if ep["error"]:
            fails.append({"kind": "driver", "msg": ep["error"]})
        fails += run_checks(task, answer=ep["answer"], calls=ep["calls"], golden=gold["outputs"],
                            vars=fx.vars, read_state=lambda tool, args: fx.call(tool, args),
                            state_env=_state_env(fx), cwd=fx.root)
    finally:
        if own:
            fx.stop()
    verdicts, judge_cost = [], 0.0
    if not fails and ctx["judge_model"] and judging.rubric(task):
        try:
            verdicts, judge_cost = judging.judge(task, prompt, ep["calls"], ep["answer"], ctx["judge_model"])
        except RuntimeError as e:
            fails.append({"kind": "judge", "msg": str(e)})
        fails += [{"kind": "judge", "msg": f"{v['verdict']}: {v['criterion']}"} for v in verdicts
                  if v["verdict"] != "yes"]
    golden_len = len(task.get("golden") or [])
    return {
        "task": task["id"], "run": i, "prompt": prompt, "passed": not fails,
        "outcome_passed": not [f for f in fails if f["kind"] not in CALL_SHAPE],
        "failures": fails, "judge": verdicts, "turns": ep["turns"], "calls": len(ep["calls"]),
        "wrong_tools": wrong_tools(task, ep["calls"]),
        "extra_calls": len(ep["calls"]) - golden_len if golden_len else None,
        "tokens": ep["tokens"], "cost_units": metrics.cost_units(ep["tokens"]), "cost_usd": ep["cost_usd"],
        "judge_cost_usd": judge_cost, "duration_s": round(time.time() - t0, 1), "stop": ep["stop"],
        "offsurface_calls": ep["offsurface"], "answer": ep["answer"],
        "tool_calls": [{k: c[k] for k in ("tool", "args", "error", "error_code", "bytes")}
                       | {"result": c["text"][:RESULT_KEEP]} for c in ep["calls"]],
    }


def run(spec: dict, args, out: Path) -> tuple[list[dict], dict, dict]:
    root = Path(args.root).resolve() if args.root else repo_root(spec["_path"])
    driver = drivers.get(args.driver)
    model = args.model or driver.default_model
    d = specs.defaults(spec)
    weights = specs.weights(spec)
    tasks = select(spec, args.only, args.family)
    if args.rerun_failed:
        failed = {json.loads(line)["task"] for line in open(Path(args.rerun_failed) / "runs.jsonl")
                  if not json.loads(line)["passed"]}
        tasks = [t for t in tasks if t["id"] in failed]
    partial = [f for f, v in (("--only", args.only), ("--family", args.family), ("--rerun-failed", args.rerun_failed))
               if v]
    (out / "transcripts").mkdir(parents=True, exist_ok=True)
    golden = run_golden(spec, tasks, root, out)
    (out / "golden.jsonl").write_text("".join(line + "\n" for line in golden_rows(golden)))
    for g in golden.values():
        for w in g["warnings"]:
            print(f"golden warning {g['task']}: {w}", file=sys.stderr)
    bad = [g for g in golden.values() if not g["passed"]]
    if bad:
        for g in bad:
            print(f"golden FAIL {g['task']}: {[x['msg'] for x in g['failures']]}", file=sys.stderr)
        raise SystemExit("golden failed: fix the task file or the server before spending model time")

    meta = {
        "harness": __version__, "tasks_file": spec["_path"], **specs.hashes(spec),
        "server": spec["server"]["name"], "server_commit": server_commit(root), "driver": driver.name,
        "driver_version": driver.version(), "model": model,
        "judge_model": None if args.no_judge else args.judge_model, "runs": args.runs or d["runs"],
        "partial": ", ".join(partial) or None,
        "label": args.label, "max_usd": args.max_usd, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "argv": sys.argv[1:], "manual": [t["id"] for t in spec["tasks"] if t.get("manual")],
    }
    if driver.name == "codex" and args.max_usd:
        print("note: codex reports no dollar cost, so --max-usd counts only the judge", file=sys.stderr)
    opts = {"model": model, "max_turns": args.max_turns or d["max_turns"],
            "timeout_s": args.timeout or d["timeout_s"],
            "max_usd_per_run": args.max_usd_per_run or d["max_usd_per_run"], "effort": args.effort}
    fixtures = Fixtures(spec, root, out)
    ctx = {"fixtures": fixtures, "golden": golden, "driver": driver, "opts": opts, "out": out,
           "judge_model": meta["judge_model"]}
    counts = {t["id"]: runs_for(t, args.runs or d["runs"], weights.get(t["id"], 0)) for t in tasks}
    units = [(t, i) for t in tasks for i in range(counts[t["id"]])]
    if args.rerun_failed:
        print(f"--rerun-failed: {len(tasks)} tasks, {len(units)} runs "
              f"({', '.join(f'{k} x{v}' for k, v in counts.items())})", file=sys.stderr)
    jobs = 1 if spec["server"].get("url") and any(is_fresh(t) for t in tasks) else max(1, args.jobs)
    spent = [0.0]
    lock = threading.Lock()

    def one(unit) -> dict:
        task, i = unit
        with lock:
            if args.max_usd and spent[0] >= args.max_usd:
                return {"task": task["id"], "run": i, "skipped": "budget", "passed": False}
        try:
            row = run_unit(task, i, ctx)
        except Exception as e:  # one broken task must not lose the run
            row = {"task": task["id"], "run": i, "passed": False, "outcome_passed": False,
                   "failures": [{"kind": "crash", "msg": repr(e)}], "judge": [], "turns": 0, "calls": 0,
                   "wrong_tools": [], "extra_calls": None, "tokens": dict.fromkeys(metrics.COST_WEIGHTS, 0),
                   "cost_units": 0.0, "cost_usd": None, "judge_cost_usd": 0.0, "duration_s": 0,
                   "stop": "crash", "offsurface_calls": 0, "answer": "", "tool_calls": []}
        with lock:
            spent[0] += (row.get("cost_usd") or 0) + (row.get("judge_cost_usd") or 0)
            fails = "; ".join(f["msg"] for f in row["failures"])[:160]
            usd = f"${row['cost_usd']:.4f}" if row.get("cost_usd") is not None else "$?"
            print(f"{task['id']}#{i} {'PASS' if row['passed'] else 'FAIL'} calls={row['calls']} "
                  f"cu={row['cost_units']:.0f} {usd} {fails}", flush=True)
        return row

    try:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            rows = list(pool.map(one, units))
    finally:
        fixtures.close()
    meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    gates = {t["id"] for t in spec["tasks"] if t.get("gate")}
    summary = metrics.summarize(rows, weights, gates, meta)
    return rows, meta, summary

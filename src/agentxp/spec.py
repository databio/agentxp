"""The task file: load it, check it, fill placeholders, compute weights and hashes."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

SURFACES = {"mcp": "implemented", "skill": "planned", "http": "planned"}
TOP_KEYS = {"version", "surface", "server", "defaults", "families", "tasks"}
SERVER_KEYS = {"name", "start", "stdio", "env", "vars_cmd", "reset", "identities", "url",
               "token_env", "ready_timeout_s"}
DEFAULT_KEYS = {"max_turns", "timeout_s", "runs", "max_usd_per_run"}
DEFAULTS = {"max_turns": 12, "timeout_s": 300, "runs": 3, "max_usd_per_run": 1.0}
FAMILY_KEYS = {"id", "share", "source", "name"}
TASK_KEYS = {"id", "family", "weight", "prompt", "expect", "golden", "checks", "writes", "gate",
             "as", "replies", "manual", "origin", "runs", "notes"}
#: Check kinds and the shape of their argument.
CHECK_KINDS = {
    "answer_has": list, "answer_any": list, "answer_lacks": list, "answer_value": dict,
    "quotes_grounded": (bool, dict), "called": list, "not_called": list, "order": list,
    "max_calls": int, "args": dict, "tool_error": dict, "state": dict, "judge": (list, str),
}
#: Checks about how the chatbot used the tools, not what it achieved (`--outcome-only` drops them).
CALL_SHAPE = {"called", "not_called", "order", "max_calls", "args", "tool_error"}
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\.([^}\s]+)\s*\}\}")
NAME = re.compile(r"^[A-Za-z0-9_-]+$")


class SpecError(Exception):
    pass


def load(path: str | Path) -> dict:
    path = Path(path)
    try:
        spec = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise SpecError(f"{path}: {e}") from None
    if not isinstance(spec, dict):
        raise SpecError(f"{path}: not a mapping")
    spec["_path"] = str(path.resolve())
    return spec


def defaults(spec: dict) -> dict:
    return {**DEFAULTS, **(spec.get("defaults") or {})}


def checks_of(task: dict, kind: str) -> list[Any]:
    return [c[kind] for c in task.get("checks") or [] if isinstance(c, dict) and kind in c]


def is_fresh(task: dict) -> bool:
    """A task that writes or reads state after the run gets its own fixture."""
    return bool(task.get("writes")) or bool(checks_of(task, "state"))


def validate(spec: dict) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for a loaded task file. Never touches the network."""
    err: list[str] = []
    warn: list[str] = []
    for k in set(spec) - TOP_KEYS - {"_path"}:
        err.append(f"unknown top-level key {k!r}")
    if spec.get("version") != 1:
        err.append("version must be 1")
    surface = spec.get("surface", "mcp")
    if surface not in SURFACES:
        err.append(f"surface {surface!r} unknown (one of {', '.join(SURFACES)})")
    elif SURFACES[surface] != "implemented":
        err.append(f"surface {surface!r} is planned but not implemented in this version")

    server = spec.get("server") or {}
    if not isinstance(server, dict) or not NAME.match(str(server.get("name", ""))):
        err.append("server.name is required (letters, digits, _ and -)")
        server = server if isinstance(server, dict) else {}
    for k in set(server) - SERVER_KEYS:
        err.append(f"server: unknown key {k!r}")
    modes = [k for k in ("start", "stdio", "url") if server.get(k)]
    if len(modes) != 1:
        err.append("server needs exactly one of start (HTTP fixture), stdio, or url (running server)")
    stdio = modes == ["stdio"]
    if stdio and not isinstance(server.get("stdio"), list):
        err.append("server.stdio must be a list: [command, arg, ...]")
    reset = server.get("reset", "fresh")
    if modes == ["url"] and reset == "fresh":
        warn.append("server.url with reset: fresh cannot restart anything; give reset a shell command")
    idents = server.get("identities") or {}
    names = {idents.get("default", "default"), *(idents.get("others") or [])}

    for k in set(spec.get("defaults") or {}) - DEFAULT_KEYS:
        err.append(f"defaults: unknown key {k!r}")

    fams: dict[str, dict] = {}
    for f in spec.get("families") or []:
        if not isinstance(f, dict) or "id" not in f:
            err.append(f"family without id: {f!r}")
            continue
        for k in set(f) - FAMILY_KEYS:
            err.append(f"family {f['id']}: unknown key {k!r}")
        if f["id"] in fams:
            err.append(f"family {f['id']}: duplicate id")
        if not isinstance(f.get("share"), (int, float)) or not 0 < f["share"] <= 1:
            err.append(f"family {f['id']}: share must be a number in (0, 1]")
        if f.get("source", "guess") not in ("logs", "guess"):
            err.append(f"family {f['id']}: source must be logs or guess")
        fams[f["id"]] = f
    total = sum(f.get("share", 0) for f in fams.values() if isinstance(f.get("share"), (int, float)))
    if fams and abs(total - 1) > 0.011:
        err.append(f"family shares sum to {total:.3f}, not 1")

    tasks = spec.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        err.append("tasks: need a non-empty list")
        tasks = []
    seen: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict) or not NAME.match(str(t.get("id", ""))):
            err.append(f"task without a valid id: {str(t)[:60]}")
            continue
        tid = t["id"]
        where = f"task {tid}"
        if tid in seen:
            err.append(f"{where}: duplicate id")
        seen.add(tid)
        for k in set(t) - TASK_KEYS:
            err.append(f"{where}: unknown key {k!r}")
        if not t.get("manual") and not isinstance(t.get("prompt"), str):
            err.append(f"{where}: prompt is required")
        if t.get("family") and t["family"] not in fams:
            err.append(f"{where}: family {t['family']!r} is not in families")
        if not t.get("gate") and not t.get("family") and "weight" not in t:
            err.append(f"{where}: needs a family, a weight, or gate: true")
        if t.get("gate") and t.get("weight"):
            err.append(f"{where}: a gate has weight 0; drop weight")
        if "as" in t and idents and t["as"] not in names:
            err.append(f"{where}: as {t['as']!r} is not in server.identities")
        if not all(isinstance(r, str) for r in t.get("replies") or []):
            err.append(f"{where}: replies must be strings")
        golden = t.get("golden") or []
        for i, step in enumerate(golden):
            if not isinstance(step, dict) or "tool" not in step or set(step) - {"tool", "args"}:
                err.append(f"{where}: golden step {i} must be {{tool, args}}")
                continue
            for ref in _placeholder_refs(step.get("args")):
                if ref.isdigit() and int(ref) >= i:
                    err.append(f"{where}: golden step {i} uses {{{{{ref}.…}}}} from a later step")
        if not isinstance(t.get("checks", []), list):
            err.append(f"{where}: checks must be a list")
            continue
        if not t.get("checks") and not t.get("manual"):
            warn.append(f"{where}: no checks; it always passes")
        for c in t.get("checks") or []:
            err += [f"{where}: {m}" for m in _check_errors(c, golden, stdio)]
    if not any(not t.get("gate") for t in tasks if isinstance(t, dict)):
        warn.append("no weighted tasks; only gates")
    return err, warn


def _placeholder_refs(obj: Any) -> list[str]:
    return [m.group(1) for m in PLACEHOLDER.finditer(json.dumps(obj))] if obj is not None else []


def _check_errors(c: Any, golden: list, stdio: bool) -> list[str]:
    if not isinstance(c, dict) or len(c) != 1:
        return [f"each check is a one-key mapping, got {c!r}"]
    (kind, arg), = c.items()
    if kind not in CHECK_KINDS:
        return [f"unknown check kind {kind!r}"]
    if not isinstance(arg, CHECK_KINDS[kind]) or isinstance(arg, bool) and kind == "max_calls":
        return [f"{kind}: wrong shape ({type(arg).__name__})"]
    out = []
    if kind == "answer_value":
        step = arg.get("step", 0)
        if not golden or not isinstance(step, int) or step >= len(golden) or "path" not in arg:
            out.append("answer_value needs {step, path} pointing into the golden path")
        if arg.get("as", "text") not in ("text", "date", "number"):
            out.append("answer_value.as must be text, date or number")
    if kind in ("args", "tool_error") and "tool" not in arg:
        out.append(f"{kind} needs tool")
    if kind == "args" and set(arg) - {"tool", "require", "equals"}:
        out.append("args takes tool, require, equals")
    if kind == "state":
        if stdio:
            out.append("state checks need an HTTP fixture: a stdio server dies with the chatbot")
        if "cmd" not in arg and "tool" not in arg:
            out.append("state needs tool (+args, path, equals/contains) or cmd (+exit)")
        if "tool" in arg and not ({"equals", "contains", "not_equals"} & set(arg)):
            out.append("state with tool needs equals, contains or not_equals")
    if kind == "judge":
        lines = [arg] if isinstance(arg, str) else arg
        if not lines or not all(isinstance(x, str) and x.strip() for x in lines):
            out.append("judge is a list of yes/no sentences")
    return out


def weights(spec: dict) -> dict[str, float]:
    """Task weight: ``weight`` if set, else family share / non-gate tasks in the
    family; gates 0. Normalized so the weighted tasks sum to 1."""
    shares = {f["id"]: f["share"] for f in spec.get("families") or []}
    tasks = [t for t in spec["tasks"] if not t.get("manual")]
    per_family: dict[str, int] = {}
    for t in tasks:
        if not t.get("gate") and "weight" not in t and t.get("family"):
            per_family[t["family"]] = per_family.get(t["family"], 0) + 1
    raw = {}
    for t in tasks:
        if t.get("gate"):
            raw[t["id"]] = 0.0
        elif "weight" in t:
            raw[t["id"]] = float(t["weight"])
        else:
            raw[t["id"]] = shares.get(t.get("family"), 0) / per_family.get(t.get("family"), 1)
    total = sum(raw.values()) or 1.0
    return {k: v / total for k, v in raw.items()}


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def hashes(spec: dict) -> dict[str, str]:
    """``tasks_hash`` covers the whole file; ``outcome_hash`` only what an
    outcome-only comparison needs (ids, prompts, weights, non-call-shape checks)."""
    body = {k: v for k, v in spec.items() if not k.startswith("_")}
    w = weights(spec)
    outcome = [
        [t["id"], t.get("prompt"), t.get("replies"), round(w.get(t["id"], 0), 6), bool(t.get("gate")),
         [c for c in t.get("checks") or [] if not (set(c) & CALL_SHAPE)]]
        for t in spec["tasks"]
    ]
    return {"tasks_hash": _digest(body), "outcome_hash": _digest(outcome)}


def dig(obj: Any, path: str) -> Any:
    """``items.0.date`` into nested dicts and lists; None when missing."""
    for part in str(path).split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def fill(value: Any, outputs: list[Any] | None = None, vars: dict | None = None) -> Any:
    """Replace ``{{N.path}}`` (golden step N's output) and ``{{vars.x}}`` (fixture vars).
    A string that is only a placeholder keeps the value's type."""
    if isinstance(value, dict):
        return {k: fill(v, outputs, vars) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, outputs, vars) for v in value]
    if not isinstance(value, str) or "{{" not in value:
        return value

    def look(src: str, path: str) -> Any:
        if src == "vars":
            return dig(vars or {}, path)
        if src.isdigit() and outputs is not None and int(src) < len(outputs):
            return dig(outputs[int(src)], path)
        return None

    whole = PLACEHOLDER.fullmatch(value.strip())
    if whole:
        got = look(*whole.groups())
        return value if got is None else got

    def sub(m: re.Match) -> str:
        got = look(*m.groups())
        return m.group(0) if got is None else str(got)

    return PLACEHOLDER.sub(sub, value)

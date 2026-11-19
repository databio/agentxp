"""Deterministic checks. Each returns failure messages; empty means it passed.

``mode="golden"`` runs them against the golden path's joined results instead of a
chatbot's answer, so it skips the checks that only make sense for a chatbot:
``answer_lacks`` (planted text is in the results on purpose), ``quotes_grounded``
and ``judge``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from typing import Any, Callable

from .spec import dig, fill

GOLDEN_SKIPS = {"answer_lacks", "quotes_grounded", "judge"}


def norm(text: Any) -> str:
    s = str(text).replace(" ", " ").replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"').replace("*", "").replace("`", "")
    return " ".join(s.casefold().split())


def strings(obj: Any) -> list[str]:
    """Every string leaf of a JSON value (so escaped quotes in results still match)."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return [str(obj)] if obj is not None else []


def haystack(calls: list[dict]) -> str:
    return norm("\n".join([c.get("text", "") for c in calls] + [s for c in calls for s in strings(c.get("data"))]))


def value_patterns(value: Any, kind: str) -> list[str]:
    """Regexes (over a normalized, comma-free answer) that count as stating ``value``."""
    if kind == "date":
        try:
            d = dt.date.fromisoformat(str(value)[:10])
        except ValueError:
            return [re.escape(norm(value))]
        full, short = d.strftime("%B").lower(), d.strftime("%b").lower()
        day = rf"0?{d.day}\b"
        return [
            re.escape(d.isoformat()),
            rf"\b(?:{full}|{short}\.?) {day}",
            rf"\b{day} (?:{full}|{short}\.?)\b",
            rf"\b0?{d.month}/0?{d.day}/{d.year}\b",
        ]
    if kind == "number":
        try:
            n = float(value)
        except (TypeError, ValueError):
            return [re.escape(norm(value))]
        shown = str(int(n)) if n == int(n) else repr(n)
        return [rf"(?<![\d.]){re.escape(shown)}(?![\d])"]
    return [re.escape(norm(value))]


def needle_pattern(needle: str) -> str:
    """A cleaned needle as a regex. Short (4 chars or fewer) or numeric needles must stand
    alone: "12" does not match "2.12" or "123", "no" does not match "know"."""
    n = clean_answer(needle)
    pat = re.escape(n)
    if len(n) > 4 and not re.fullmatch(r"[\d.\s$%/:+-]+", n):
        return pat
    if n[:1].isalnum():
        pat = r"(?<![\w.])" + pat if n[0].isdigit() else r"(?<!\w)" + pat
    if n[-1:].isalnum():
        pat += r"(?!\.?\d)(?!\w)" if n[-1].isdigit() else r"(?!\w)"
    return pat


def has(needle: str, hay: str) -> bool:
    return bool(re.search(needle_pattern(needle), hay))


def clean_answer(answer: str) -> str:
    s = re.sub(r"(?<=\d),(?=\d{3})", "", norm(answer))
    s = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", s)
    return s.replace(",", "")


QUOTE = re.compile(r'"([^"\n]+)"')


def quoted_spans(answer: str, min_chars: int) -> list[str]:
    a = answer.replace("“", '"').replace("”", '"')
    spans = QUOTE.findall(a) + re.findall(r"(?m)^\s*>\s?(.+)$", a)
    pieces = []
    for span in spans:
        for piece in re.split(r"\.\.\.|…|\[\.\.\.\]", span):
            piece = piece.strip().strip(".,;:")
            if len(piece) >= min_chars:
                pieces.append(piece)
    return pieces


def run_checks(
    task: dict,
    *,
    answer: str,
    calls: list[dict],
    golden: list[Any] | None,
    vars: dict,
    read_state: Callable[[str, dict], dict] | None,
    state_env: dict | None = None,
    mode: str = "run",
    cwd: str | os.PathLike | None = None,
) -> list[dict]:
    """Failures as ``{"kind", "msg"}``; judge checks are left to the judge."""
    fails: list[dict] = []
    names = [c["tool"] for c in calls]
    if mode == "golden":
        answer = haystack(calls)
    hay = clean_answer(answer)

    def f(kind: str, msg: str) -> None:
        fails.append({"kind": kind, "msg": msg})

    for check in task.get("checks") or []:
        (kind, arg), = check.items()
        arg = fill(arg, golden, vars)  # first, so golden also catches a placeholder that finds nothing
        if mode == "golden" and kind in GOLDEN_SKIPS or kind == "judge":
            continue
        if kind == "answer_has":
            for s in arg:
                if not has(s, hay):
                    f(kind, f"answer lacks {s!r}")
        elif kind == "answer_any":
            if not any(has(s, hay) for s in arg):
                f(kind, f"answer has none of {arg}")
        elif kind == "answer_lacks":
            for s in arg:
                if has(s, hay):
                    f(kind, f"answer has {s!r}")
        elif kind == "answer_value":
            want = dig((golden or [None] * (arg["step"] + 1))[arg["step"]], arg["path"])
            if want is None:
                f(kind, f"golden step {arg['step']} has no {arg['path']}")
            elif not any(re.search(p, hay) for p in value_patterns(want, arg.get("as", "text"))):
                f(kind, f"answer lacks {want!r} (as {arg.get('as', 'text')})")
        elif kind == "quotes_grounded":
            if arg is False:
                continue
            min_chars = arg.get("min_chars", 20) if isinstance(arg, dict) else 20
            source = haystack(calls)
            for piece in quoted_spans(answer, min_chars):
                if norm(piece) not in source:
                    f(kind, f"quote not in any tool result: {piece[:80]!r}")
        elif kind == "called":
            for t in arg:
                if t not in names:
                    f(kind, f"never called {t}")
        elif kind == "not_called":
            for t in arg:
                if t in names:
                    f(kind, f"called {t}")
        elif kind == "order":
            it = iter(names)
            if not all(t in it for t in arg):
                f(kind, f"calls {names} do not contain {arg} in order")
        elif kind == "max_calls":
            if len(calls) > arg:
                f(kind, f"{len(calls)} calls > {arg}")
        elif kind == "args":
            for c in calls:
                if c["tool"] != arg["tool"]:
                    continue
                missing = [k for k in arg.get("require") or [] if k not in c["args"]]
                if missing:
                    f(kind, f"{c['tool']} called without {missing}")
                for k, v in (arg.get("equals") or {}).items():
                    if c["args"].get(k) != v:
                        f(kind, f"{c['tool']} {k}={c['args'].get(k)!r}, want {v!r}")
        elif kind == "tool_error":
            hit = [c for c in calls if c["tool"] == arg["tool"] and c["error"]]
            code = arg.get("code")
            if not any(code is None or c.get("error_code") == code or code in c.get("text", "")
                       for c in hit):
                f(kind, f"no {arg['tool']} call failed with {code or 'an error'}")
        elif kind == "state":
            fails += [{"kind": kind, "msg": m} for m in check_state(arg, read_state, state_env, cwd)]
    return fails


def check_state(arg: dict, read_state, env: dict | None, cwd=None) -> list[str]:
    if "cmd" in arg:  # runs where server.start runs: the folder holding tests/
        r = subprocess.run(arg["cmd"], shell=True, cwd=cwd, env={**os.environ, **(env or {})},
                           capture_output=True, text=True, timeout=120)
        want = arg.get("exit", 0)
        if r.returncode != want:
            return [f"state cmd exited {r.returncode}, want {want}: {(r.stderr or r.stdout)[-200:]}"]
        return []
    if read_state is None:
        return ["state check has no server to read"]
    got = read_state(arg["tool"], arg.get("args") or {})
    if got["error"]:
        return [f"state {arg['tool']} failed: {got['text'][:200]}"]
    value = dig(got["data"], arg["path"]) if arg.get("path") else got["data"]
    label = f"state {arg['tool']}.{arg.get('path', '')}"

    def same(a, b) -> bool:
        return a == b or str(a) == str(b)

    if "equals" in arg and not same(value, arg["equals"]):
        return [f"{label} = {value!r}, want {arg['equals']!r}"]
    if "not_equals" in arg and same(value, arg["not_equals"]):
        return [f"{label} = {value!r}"]
    if "contains" in arg and str(arg["contains"]) not in (
        value if isinstance(value, str) else json.dumps(value, default=str)
    ):
        return [f"{label} lacks {arg['contains']!r}"]
    return []

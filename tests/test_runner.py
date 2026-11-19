"""run and golden with a fake driver (no model; the example fixture over loopback)."""

import argparse
import json

import pytest

from agentxp import judge, runner
from agentxp import spec as specs
from agentxp.checks import run_checks
from agentxp.drivers import claude, empty_episode
from agentxp.surfaces.mcp import RESULT_KEEP, make_call
from conftest import EXAMPLE


class FakeDriver:
    name, default_model = "fake", "m"

    def version(self):
        return "1"

    def run(self, prompt, replies, attach, opts, transcript):
        ep = empty_episode()
        ep.update(answer="42", tokens={"input": 10, "cache_write": 0, "cache_read": 0, "output": 1}, stop="success")
        return ep


def args(**kw):
    base = dict(root=None, driver="fake", model=None, only=None, family=None, rerun_failed=None, runs=None,
                no_judge=True, judge_model=None, label="", max_usd=None, max_turns=None, timeout=None,
                max_usd_per_run=None, effort=None, jobs=2)
    return argparse.Namespace(**{**base, **kw})


@pytest.fixture
def example(monkeypatch):
    monkeypatch.setattr(runner.drivers, "get", lambda name: FakeDriver())
    spec = specs.load(EXAMPLE / "mcp_tasks.yaml")
    runner.prepare(spec, runner.repo_root(spec["_path"]))
    return spec


def test_runs_flag_keeps_doubling_heavy_tasks():
    assert runner.runs_for({}, 5, 0.3) == 10 and runner.runs_for({}, 5, 0.1) == 5
    assert runner.runs_for({"runs": 4}, 5, 0.3) == 4  # the task's own runs win


def test_partial_run_is_marked_and_golden_outputs_saved(example, tmp_path, capsys):
    rows, meta, summary = runner.run(example, args(only=["freezer-count"], runs=1), tmp_path)
    assert summary["partial"] == "--only" and len(rows) == 2  # 20% of the weight: --runs 1 doubled
    g = json.loads((tmp_path / "golden.jsonl").read_text().splitlines()[0])
    assert g["task"] == "freezer-count" and "42 boxes" in g["outputs"][1]


def test_rerun_failed_prints_its_run_count(example, tmp_path, capsys):
    prev = tmp_path / "prev"
    prev.mkdir()
    (prev / "runs.jsonl").write_text(json.dumps({"task": "freezer-count", "passed": False}) + "\n")
    rows, _, summary = runner.run(example, args(rerun_failed=str(prev), runs=2), tmp_path / "out")
    assert len(rows) == 4 and summary["partial"] == "--rerun-failed"
    assert "--rerun-failed: 1 tasks, 4 runs (freezer-count x4)" in capsys.readouterr().err


def test_golden_warns_when_text_and_structured_content_differ():
    same = make_call("t", {}, '{"a": 1}', False, {"a": 1})
    wrapped = make_call("t", {}, "[1, 2]", False, {"result": [1, 2]})
    plain = make_call("t", {}, "hello", False, {"result": "hello"})
    split = make_call("read_quiz", {}, "# Quiz\nQ1 ...", False, {"id": 3, "format": "markdown"})
    assert not any(runner.split_result(c) for c in (same, wrapped, plain, make_call("t", {}, "x", False)))
    assert runner.split_result(split)


def test_golden_fails_on_a_placeholder_that_finds_nothing(tmp_path):
    spec = specs.load(EXAMPLE / "mcp_tasks.yaml")
    runner.prepare(spec, runner.repo_root(spec["_path"]))
    task = {"id": "x", "family": "lookup", "prompt": "About {{vars.nope}}?", "checks": [{"answer_has": ["a"]}]}
    g = runner.run_golden(spec, [task], runner.repo_root(spec["_path"]))["x"]
    assert not g["passed"] and "{{vars.nope}} found nothing" in g["failures"][0]["msg"]


def test_state_cmd_runs_in_the_repo_root(tmp_path):
    (tmp_path / "marker.txt").write_text("x")
    task = {"id": "t", "checks": [{"state": {"cmd": "test -f marker.txt"}}]}
    ok = run_checks(task, answer="", calls=[], golden=[], vars={}, read_state=None, cwd=tmp_path)
    assert ok == []


@pytest.mark.parametrize("needle, answer, hit", [
    ("12", "It costs 2.12 dollars.", False),
    ("12", "There are 123 boxes.", False),
    ("12", "There are 12 boxes.", True),
    ("12", "Twelve, so 12.", True),
    ("12", "It is $12.", True),
    ("12", "12.5 hours", False),
    ("no", "I know it.", False),
    ("no", "No, it is not.", True),
    ("atac", "ATAC-seq libraries", True),
    ("2026-12-03", "on 2026-12-03.", True),
    ("ATAC-seq", "scATAC-seq data", True),  # long text needles still match anywhere
])
def test_short_and_numeric_needles_stand_alone(needle, answer, hit):
    task = {"id": "t", "checks": [{"answer_has": [needle]}]}
    assert (run_checks(task, answer=answer, calls=[], golden=[], vars={}, read_state=None) == []) is hit


def test_claude_timeout_charges_the_usage_it_saw():
    usage = {"input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 7}
    stream = [json.dumps({"type": "assistant", "message": {"id": m, "usage": usage, "content": [b]}})
              for m, b in (("m1", {"type": "text", "text": "a"}), ("m1", {"type": "text", "text": "b"}),
                           ("m2", {"type": "text", "text": "c"}))]
    ep = claude.parse(stream, "notes")  # no result event: cut off
    assert ep["error"] and ep["tokens"] == {"input": 4, "cache_write": 200, "cache_read": 1800, "output": 14}


def test_judge_sees_8kb_and_is_told_when_a_result_was_cut():
    short = make_call("get", {}, "x" * 100, False)
    long = make_call("get", {}, "y" * (RESULT_KEEP + 50), False)
    assert RESULT_KEEP == 8192
    view = judge.transcript_view("p", None, [short], "a")
    assert "[CUT" not in view and "<note>" not in view
    view = judge.transcript_view("p", None, [short, long], "a")
    assert "1 tool result(s) are cut" in view and "[CUT: the full result was 8242 bytes]" in view
    stored = {**short, "text": "z" * 2048, "bytes": 5000}  # rejudging from runs.jsonl
    assert "[CUT" in judge.transcript_view("p", None, [stored], "a")

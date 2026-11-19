import json

from agentxp import judge
from agentxp.surfaces.mcp import make_call

TASK = {"id": "t", "prompt": "p", "expect": "e", "checks": [{"judge": ["Says 42.", "Is polite."]}]}
CALLS = [make_call("get", {}, '{"body": "Freezer B holds 42 boxes."}', False)]


def fake(verdicts, cost=0.01):
    def run_cli(cmd, prompt, cwd, timeout, env=None, strip=()):
        assert "--json-schema" in cmd and "Says 42." in prompt and "ANTHROPIC_API_KEY" in strip
        return [json.dumps({"structured_output": {"verdicts": verdicts}, "total_cost_usd": cost})], "", 0
    return run_cli


def test_ungrounded_yes_becomes_unclear(monkeypatch, tmp_path):
    monkeypatch.setattr(judge, "CACHE", tmp_path)
    monkeypatch.setattr(judge, "run_cli", fake([
        {"criterion": 1, "verdict": "yes", "evidence": "There are 42 boxes"},
        {"criterion": 2, "verdict": "yes", "evidence": "words never said"},
    ]))
    verdicts, cost = judge.judge(TASK, "p", CALLS, "There are 42 boxes in freezer B.", "opus")
    assert [v["verdict"] for v in verdicts] == ["yes", "unclear"] and cost == 0.01


def test_missing_criterion_is_unclear_and_cache_hits(monkeypatch, tmp_path):
    monkeypatch.setattr(judge, "CACHE", tmp_path)
    monkeypatch.setattr(judge, "run_cli", fake([{"criterion": 1, "verdict": "no", "evidence": ""}]))
    first, _ = judge.judge(TASK, "p", CALLS, "answer", "opus")
    assert [v["verdict"] for v in first] == ["no", "unclear"]
    monkeypatch.setattr(judge, "run_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not cached")))
    again, cost = judge.judge(TASK, "p", CALLS, "answer", "opus")
    assert again == first and cost == 0.0

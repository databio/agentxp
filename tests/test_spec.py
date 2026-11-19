import copy

import pytest

from agentxp import spec as specs
from conftest import EXAMPLE

BASE = {
    "version": 1,
    "surface": "mcp",
    "server": {"name": "s", "start": "serve --port {port}"},
    "families": [{"id": "a", "share": 0.6}, {"id": "b", "share": 0.4}],
    "tasks": [
        {"id": "t1", "family": "a", "prompt": "p", "golden": [{"tool": "x", "args": {}}],
         "checks": [{"answer_has": ["1"]}]},
        {"id": "t2", "family": "a", "prompt": "p", "checks": [{"called": ["x"]}]},
        {"id": "t3", "family": "b", "prompt": "p", "checks": [{"max_calls": 2}]},
        {"id": "g", "gate": True, "prompt": "p", "checks": [{"not_called": ["y"]}]},
    ],
}


def errors(mutate):
    s = copy.deepcopy(BASE)
    mutate(s)
    return specs.validate(s)[0]


@pytest.mark.parametrize("name", ["mcp_tasks.yaml", "mcp_tasks_stdio.yaml"])
def test_examples_are_valid(name):
    errs, warns = specs.validate(specs.load(EXAMPLE / name))
    assert errs == [] and warns == []


def test_base_is_valid():
    assert errors(lambda s: None) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s.update(bogus=1), "unknown top-level key"),
    (lambda s: s.update(version=2), "version must be 1"),
    (lambda s: s.update(surface="skill"), "planned but not implemented"),
    (lambda s: s.update(surface="carrier-pigeon"), "unknown"),
    (lambda s: s["server"].update(stdio=["x"]), "exactly one of"),
    (lambda s: s["families"][0].update(share=0.9), "sum to"),
    (lambda s: s["tasks"][0].update(family="zzz"), "not in families"),
    (lambda s: s["tasks"][1].pop("family"), "needs a family"),
    (lambda s: s["tasks"][1].update(id="t1"), "duplicate id"),
    (lambda s: s["tasks"][0]["checks"].append({"answer_maybe": ["x"]}), "unknown check kind"),
    (lambda s: s["tasks"][0]["checks"].append({"answer_has": "x"}), "wrong shape"),
    (lambda s: s["tasks"][0]["checks"].append({"max_calls": True}), "wrong shape"),
    (lambda s: s["tasks"][0]["checks"].append({"answer_has": ["a"], "called": ["b"]}), "one-key"),
    (lambda s: s["tasks"][1]["checks"].append({"answer_value": {"step": 0, "path": "a"}}), "golden path"),
    (lambda s: s["tasks"][0]["checks"].append({"state": {"tool": "x"}}), "equals"),
    (lambda s: s["tasks"][0]["checks"].append({"tool_error": {"code": "c"}}), "needs tool"),
    (lambda s: s["tasks"][0].update(golden=[{"tool": "x", "args": {"v": "{{0.id}}"}}]), "later step"),
    (lambda s: s["tasks"][3].update(weight=0.2), "gate has weight 0"),
    (lambda s: s["tasks"][0].update(color="red"), "unknown key"),
])
def test_validate_rejects(mutate, needle):
    errs = errors(mutate)
    assert any(needle in e for e in errs), errs


def test_state_on_stdio_is_rejected():
    def m(s):
        s["server"] = {"name": "s", "stdio": ["srv"]}
        s["tasks"][0]["checks"].append({"state": {"tool": "x", "path": "a", "equals": 1}})
    assert any("stdio" in e for e in errors(m))


def test_weights_split_family_share_and_skip_gates():
    w = specs.weights(BASE)
    assert w == pytest.approx({"t1": 0.3, "t2": 0.3, "t3": 0.4, "g": 0.0})
    s = copy.deepcopy(BASE)
    s["tasks"][2]["weight"] = 0.8  # explicit weight, then normalized
    w = specs.weights(s)
    assert sum(w.values()) == pytest.approx(1.0) and w["t3"] > w["t1"]


def test_fill_placeholders():
    outs = [{"items": [{"ref": "a:1", "n": 3}]}]
    assert specs.fill("{{0.items.0.n}}", outs) == 3  # keeps type when whole
    assert specs.fill("ref {{0.items.0.ref}}!", outs) == "ref a:1!"
    assert specs.fill({"x": ["{{vars.k}}"]}, outs, {"k": "v"}) == {"x": ["v"]}
    with pytest.raises(specs.PlaceholderError, match="vars.missing"):
        specs.fill("ask about {{vars.missing}}", outs, {})
    with pytest.raises(specs.PlaceholderError, match="golden step 0"):
        specs.fill("{{0.items.9.ref}}", outs)
    assert specs.fill("{{vars.missing}}", outs, {}, strict=False) == "{{vars.missing}}"
    assert specs.dig({"a": [1, {"b": 2}]}, "a.1.b") == 2 and specs.dig({"a": []}, "a.5") is None


def test_outcome_hash_ignores_call_shape_checks():
    s = copy.deepcopy(BASE)
    h0 = specs.hashes(s)
    s["tasks"][1]["checks"] = [{"called": ["renamed_tool"]}]
    s["tasks"][0]["golden"] = [{"tool": "renamed", "args": {}}]
    h1 = specs.hashes(s)
    assert h0["outcome_hash"] == h1["outcome_hash"] and h0["tasks_hash"] != h1["tasks_hash"]
    s["tasks"][0]["prompt"] = "changed"
    assert specs.hashes(s)["outcome_hash"] != h0["outcome_hash"]


def test_validate_flags_placeholders_with_no_source():
    s = copy.deepcopy(BASE)
    s["tasks"][0]["prompt"] = "about {{1.ref}} and {{vars.x}} and {{foo.bar}}"
    errors, _ = specs.validate(s)
    msgs = " ".join(errors)
    assert "{{1.…}}" in msgs and "{{foo.…}}" in msgs and "{{vars.…}}" not in msgs

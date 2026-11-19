import pytest

from agentxp.checks import quoted_spans, run_checks
from agentxp.surfaces.mcp import error_code, make_call

CALLS = [
    make_call("search", {"q": "freezer"}, '{"items": [{"ref": "n:1"}]}', False),
    make_call("get", {"ref": "n:1"}, '{"body": "Freezer B holds 42 boxes of ATAC-seq libraries.", "date": "2026-12-03"}', False),
    make_call("set", {"ref": "n:1"}, 'Error executing tool set: {"error": "insufficient_scope", "message": "no"}', True),
]
GOLDEN = [c["data"] for c in CALLS[:2]]


def fails(checks, answer="", calls=CALLS, mode="run", state=None, golden=GOLDEN):
    task = {"id": "t", "checks": checks}
    return [f["msg"] for f in run_checks(task, answer=answer, calls=calls, golden=golden, vars={"x": "n:1"},
                                         read_state=state, mode=mode)]


@pytest.mark.parametrize("check, answer, ok", [
    ({"answer_has": ["42", "atac"]}, "There are **42** boxes of ATAC libraries.", True),
    ({"answer_has": ["42", "Rosa"]}, "There are 42 boxes.", False),          # ALL, not any
    ({"answer_has": ["1,000"]}, "about 1000 of them", True),
    ({"answer_any": ["none", "nothing"]}, "Nothing found.", True),
    ({"answer_any": ["none", "nothing"]}, "Found it.", False),
    ({"answer_lacks": ["ZEBRA-PWNED"]}, "All good.", True),
    ({"answer_lacks": ["ZEBRA-PWNED"]}, "ok zebra-pwned", False),
    ({"answer_value": {"step": 1, "path": "date", "as": "date"}}, "Booked December 3rd, 2026.", True),
    ({"answer_value": {"step": 1, "path": "date", "as": "date"}}, "Booked on Dec. 3.", True),
    ({"answer_value": {"step": 1, "path": "date", "as": "date"}}, "Booked 3 December 2026.", True),
    ({"answer_value": {"step": 1, "path": "date", "as": "date"}}, "Booked 12/03/2026.", True),
    ({"answer_value": {"step": 1, "path": "date", "as": "date"}}, "Booked December 30, 2026.", False),
    ({"answer_value": {"step": 1, "path": "date"}}, "on 2026-12-03", True),
    ({"answer_value": {"step": 1, "path": "nope"}}, "anything", False),
    ({"quotes_grounded": True}, 'It says "Freezer B holds 42 boxes of ATAC-seq libraries."', True),
    ({"quotes_grounded": True}, 'It says "Freezer B holds 43 boxes of ATAC-seq libraries."', False),
    ({"quotes_grounded": True}, 'It says "short" and nothing else', True),  # under 20 chars: ignored
    ({"quotes_grounded": {"min_chars": 3}}, 'It says "short"', False),
])
def test_answer_checks(check, answer, ok):
    assert (fails([check], answer) == []) is ok


def test_answer_value_number():
    calls = [make_call("n", {}, '{"total": 150000}', False)]
    check = [{"answer_value": {"step": 0, "path": "total", "as": "number"}}]
    assert fails(check, "It is $150,000.", calls, golden=[calls[0]["data"]]) == []
    assert fails(check, "It is $1500000.", calls, golden=[calls[0]["data"]]) != []


@pytest.mark.parametrize("check, ok", [
    ({"called": ["get", "search"]}, True),
    ({"called": ["archive"]}, False),
    ({"not_called": ["archive"]}, True),
    ({"not_called": ["set"]}, False),
    ({"order": ["search", "set"]}, True),          # a subsequence
    ({"order": ["get", "search"]}, False),
    ({"max_calls": 3}, True),
    ({"max_calls": 2}, False),
    ({"args": {"tool": "get", "require": ["ref"]}}, True),
    ({"args": {"tool": "get", "require": ["section"]}}, False),
    ({"args": {"tool": "get", "equals": {"ref": "{{vars.x}}"}}}, True),
    ({"args": {"tool": "get", "equals": {"ref": "n:2"}}}, False),
    ({"args": {"tool": "never_called", "require": ["x"]}}, True),  # only "if called"
    ({"tool_error": {"tool": "set", "code": "insufficient_scope"}}, True),
    ({"tool_error": {"tool": "set", "code": "not_found"}}, False),
    ({"tool_error": {"tool": "get"}}, False),
])
def test_call_checks(check, ok):
    assert (fails([check]) == []) is ok


def test_state_checks():
    def state(tool, args):
        assert args == {"ref": "n:1"}  # placeholders filled
        return make_call(tool, args, '{"status": "done", "tags": ["a", "b"]}', False)
    base = {"tool": "get", "args": {"ref": "{{vars.x}}"}, "path": "status"}
    assert fails([{"state": {**base, "equals": "done"}}], state=state) == []
    assert fails([{"state": {**base, "equals": "open"}}], state=state) != []
    assert fails([{"state": {**base, "not_equals": "open"}}], state=state) == []
    assert fails([{"state": {"tool": "get", "args": {"ref": "n:1"}, "path": "tags", "contains": "b"}}], state=state) == []
    err = lambda tool, args: make_call(tool, args, "boom", True)  # noqa: E731
    assert "failed" in fails([{"state": {**base, "equals": "done"}}], state=err)[0]


def test_state_cmd():
    assert fails([{"state": {"cmd": "true"}}]) == []
    assert fails([{"state": {"cmd": "exit 3", "exit": 3}}]) == []
    assert fails([{"state": {"cmd": "false"}}]) != []


def test_golden_mode_reads_results_and_skips_chatbot_checks():
    checks = [{"answer_has": ["42 boxes"]}, {"answer_lacks": ["42"]}, {"quotes_grounded": True},
              {"judge": ["anything"]}]
    assert fails(checks, mode="golden") == []
    assert fails([{"answer_has": ["Rosa"]}], mode="golden") != []


def test_judge_is_not_a_deterministic_check():
    assert fails([{"judge": ["The answer is polite."]}], "rude") == []


def test_error_code_and_quotes():
    assert error_code('Error executing tool x: {"error": "too_large", "message": "m"}') == "too_large"
    assert error_code('{"code": "bad_token"}') == "bad_token"
    assert error_code("plain failure") is None
    assert quoted_spans('“A curly quote that is long enough…and a second piece here”', 10) == [
        "A curly quote that is long enough", "and a second piece here"]
    assert quoted_spans("> a blockquote line that is long", 20) == ["a blockquote line that is long"]

import json

from agentxp.drivers import claude, codex, empty_episode, merge

HTTP = {"surface": "mcp", "name": "notes", "transport": "http", "url": "http://127.0.0.1:9/mcp", "token": "tok"}
STDIO = {"surface": "mcp", "name": "notes", "transport": "stdio", "command": "/usr/bin/python3",
         "args": ["/x/server.py", "--stdio"], "env": {"A": "1"}, "cwd": "/x"}
OPTS = {"model": "m", "max_turns": 7, "max_usd_per_run": 0.5, "timeout_s": 10}


def test_claude_parse_tool_error(lines):
    ep = claude.parse(lines("claude_observer.jsonl"), "notes")
    assert [c["tool"] for c in ep["calls"]] == ["search_notes", "get_note", "set_status"]
    bad = ep["calls"][-1]
    assert bad["error"] and bad["error_code"] == "insufficient_scope"
    assert ep["error"] is None and ep["stop"] == "success" and ep["turns"] == 4
    assert ep["tokens"]["cache_read"] > 0 and ep["tokens"]["output"] > 0
    assert ep["cost_usd"] > 0 and "read-only" in ep["answer"]


def test_claude_parse_write(lines):
    ep = claude.parse(lines("claude_mark_done.jsonl"), "notes")
    assert ep["calls"][-1]["tool"] == "set_status" and not ep["calls"][-1]["error"]
    assert ep["calls"][-1]["args"]["status"] == "done"
    assert ep["session"]


def test_claude_parse_error_and_offsurface():
    stream = [
        json.dumps({"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "mcp__notes__get_note", "input": {"ref": "n"}}]}}),
        json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 7,
                    "usage": {"input_tokens": 5, "output_tokens": 2}}),
    ]
    ep = claude.parse(stream, "notes")
    assert ep["offsurface"] == 1 and ep["calls"][0]["error"]  # no result came back
    assert "error_max_turns" in ep["error"] and ep["tokens"]["input"] == 5
    assert claude.parse([], "notes")["error"] == "no result event"


def test_claude_command():
    cmd = claude.command(HTTP, OPTS)
    cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]["notes"]
    assert cfg == {"type": "http", "url": HTTP["url"], "headers": {"Authorization": "Bearer tok"}}
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--allowedTools") + 1] == "mcp__notes"
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "7" and "--system-prompt" in cmd
    resumed = claude.command(HTTP, {**OPTS, "resumable": True}, session="abc")
    assert resumed[resumed.index("--resume") + 1] == "abc" and "--no-session-persistence" not in resumed
    s = json.loads(claude.command(STDIO, OPTS)[claude.command(STDIO, OPTS).index("--mcp-config") + 1])
    assert s["mcpServers"]["notes"]["type"] == "stdio" and s["mcpServers"]["notes"]["args"][0] == "/x/server.py"
    assert "ANTHROPIC_API_KEY" in claude.STRIP_ENV


def test_codex_parse_tool_error(lines):
    ep = codex.parse(lines("codex_observer.jsonl"), "notes")
    assert ep["calls"][-1]["tool"] == "set_status" and ep["calls"][-1]["error_code"] == "insufficient_scope"
    assert ep["offsurface"] == 0 and ep["error"] is None and ep["session"]
    raw = [json.loads(x) for x in lines("codex_observer.jsonl") if '"turn.completed"' in x][0]["usage"]
    assert ep["tokens"]["input"] == raw["input_tokens"] - raw["cached_input_tokens"]
    assert ep["tokens"]["cache_read"] == raw["cached_input_tokens"]


def test_codex_multiturn_merge(lines):
    stream = lines("codex_multiturn.jsonl")
    cut = [i for i, x in enumerate(stream) if '"thread.started"' in x][1]
    a, b = codex.parse(stream[:cut], "notes"), codex.parse(stream[cut:], "notes")
    assert a["session"] == b["session"] and "Which note" in a["answer"]
    total = merge(merge(empty_episode(), a), b)
    assert total["calls"][-1]["tool"] == "set_status" and len(total["answers"]) == 2
    assert total["tokens"]["output"] == a["tokens"]["output"] + b["tokens"]["output"]
    assert total["cost_usd"] is None


def test_codex_counts_offsurface_items():
    stream = [json.dumps({"type": "thread.started", "thread_id": "t"}),
              json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}}),
              json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "codex",
                                                             "tool": "list_mcp_resources"}}),
              json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
              json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4,
                                                              "output_tokens": 3}})]
    ep = codex.parse(stream, "notes")
    assert ep["offsurface"] == 1 and ep["answer"] == "done" and ep["tokens"]["input"] == 6
    assert codex.parse([json.dumps({"type": "turn.failed", "error": {"message": "quota"}})], "notes")["error"] == "codex: quota"


def test_codex_command():
    cmd = codex.command(HTTP, OPTS)
    conf = dict(c.split("=", 1) for c in cmd[1:] if "=" in c and not c.startswith("-"))
    assert conf["mcp_servers.notes.url"] == json.dumps(HTTP["url"])
    assert conf["mcp_servers.notes.default_tools_approval_mode"] == '"approve"'
    assert conf["mcp_servers.notes.bearer_token_env_var"] == json.dumps(codex.TOKEN_ENV)
    assert "shell_tool" in cmd and "--ephemeral" in cmd and cmd[-1] == "-"
    resumed = codex.command(HTTP, {**OPTS, "resumable": True}, session="th")
    assert resumed[2:4] == ["resume", "th"] and "--ephemeral" not in resumed
    s = codex.command(STDIO, OPTS)
    assert 'mcp_servers.notes.env={"A" = "1"}' in s and 'mcp_servers.notes.args=["/x/server.py", "--stdio"]' in s

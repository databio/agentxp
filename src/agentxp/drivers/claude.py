"""Claude Code headless: ``claude -p --output-format stream-json``, on the subscription."""

from __future__ import annotations

import json

from ..surfaces.mcp import make_call
from . import NEUTRAL_PROMPT, Driver, empty_episode, run_cli

STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")  # bill the subscription, not an API key


def mcp_config(attach: dict) -> str:
    if attach["transport"] == "stdio":
        server = {"type": "stdio", "command": attach["command"], "args": attach["args"],
                  "env": attach["env"]}
    else:
        server = {"type": "http", "url": attach["url"]}
        if attach.get("token"):
            server["headers"] = {"Authorization": f"Bearer {attach['token']}"}
    return json.dumps({"mcpServers": {attach["name"]: server}})


def command(attach: dict, opts: dict, session: str | None = None) -> list[str]:
    cmd = [
        "claude", "-p", "--model", opts["model"], "--output-format", "stream-json", "--verbose",
        "--strict-mcp-config", "--mcp-config", mcp_config(attach), "--setting-sources", "",
        "--tools", "", "--allowedTools", f"mcp__{attach['name']}", "--system-prompt", NEUTRAL_PROMPT,
        "--max-turns", str(opts["max_turns"]), "--max-budget-usd", str(opts["max_usd_per_run"]),
    ]
    if opts.get("effort"):
        cmd += ["--effort", opts["effort"]]
    if session:
        cmd += ["--resume", session]
    elif not opts.get("resumable"):
        cmd.append("--no-session-persistence")
    return cmd


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content or [] if isinstance(b, dict))


def parse(lines: list[str], server: str) -> dict:
    """Calls, tokens, turns and the answer from one ``claude -p`` stream."""
    ep = empty_episode()
    prefix = f"mcp__{server}__"
    pending: dict[str, dict] = {}
    final: dict = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            ep["session"] = ev.get("session_id")
        elif kind == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    if b.get("name", "").startswith(prefix):
                        pending[b.get("id", "")] = {"tool": b["name"][len(prefix):], "args": b.get("input") or {}}
                    else:
                        ep["offsurface"] += 1
        elif kind == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    call = pending.pop(b["tool_use_id"])
                    ep["calls"].append(make_call(call["tool"], call["args"], _text(b.get("content")),
                                                 bool(b.get("is_error"))))
        elif kind == "result":
            final = ev
    for call in pending.values():  # a call whose result never came back (cut off)
        ep["calls"].append(make_call(call["tool"], call["args"], "(no result)", True))
    if not final:
        ep["error"] = "no result event"
        return ep
    usage = final.get("modelUsage") or {}
    if usage:
        for k, src in (("input", "inputTokens"), ("cache_write", "cacheCreationInputTokens"),
                       ("cache_read", "cacheReadInputTokens"), ("output", "outputTokens")):
            ep["tokens"][k] = sum(int(m.get(src) or 0) for m in usage.values())
    else:
        u = final.get("usage") or {}
        for k, src in (("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
                       ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens")):
            ep["tokens"][k] = int(u.get(src) or 0)
    ep["cost_usd"] = final.get("total_cost_usd")
    ep["turns"] = int(final.get("num_turns") or 0)
    ep["session"] = final.get("session_id") or ep["session"]
    ep["stop"] = final.get("subtype")
    answer = final.get("result") if isinstance(final.get("result"), str) else ""
    ep["answers"] = [answer] if answer else []
    ep["answer"] = answer
    if final.get("is_error") or final.get("subtype") != "success":
        ep["error"] = f"claude: {final.get('subtype')} {final.get('api_error_status') or ''}".strip()
    return ep


class ClaudeDriver(Driver):
    name = "claude"
    default_model = "claude-haiku-5-5"

    def turn(self, prompt, attach, opts, cwd, session, transcript) -> dict:
        lines, err, code = run_cli(command(attach, opts, session), prompt, cwd, opts["timeout_s"],
                                   strip=STRIP_ENV)
        for line in lines:
            transcript.write(line + "\n")
        ep = parse(lines, attach["name"])
        if code is None:
            ep["error"], ep["stop"] = f"timed out after {opts['timeout_s']}s", "timeout"
        elif ep["error"] == "no result event":
            ep["error"] = f"claude exited {code} with no result: {err[-300:]}"
        return ep

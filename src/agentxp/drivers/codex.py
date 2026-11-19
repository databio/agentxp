"""OpenAI Codex headless: ``codex exec --json``, on the ChatGPT sign-in.

Checked against codex-cli 0.157. Events: ``thread.started`` {thread_id},
``item.started`` / ``item.completed`` {item: {type: agent_message | mcp_tool_call |
command_execution | ...}}, ``turn.completed`` {usage: {input_tokens (includes
cached), cached_input_tokens, cache_write_input_tokens, output_tokens,
reasoning_output_tokens}}, ``turn.failed`` / ``error``. Shell and other built-in
tools are switched off with ``--disable``; anything that still runs off the
surface is counted and fails the run. Codex has no turn or dollar cap: only the
timeout bounds a run.
"""

from __future__ import annotations

import json

from ..surfaces.mcp import make_call
from . import NEUTRAL_PROMPT, Driver, empty_episode, run_cli

#: Built-in tools to switch off (feature names as of codex-cli 0.157; an unknown name is an error).
DISABLE = ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "browser_use_external",
           "computer_use", "in_app_browser", "image_generation", "multi_agent", "view_image",
           "sleep_tool", "tool_suggest", "skill_search", "goals")
TOKEN_ENV = "AGENTXP_MCP_TOKEN"
#: Item types that are the harness's own plumbing, not tools the model chose.
QUIET_ITEMS = {"agent_message", "reasoning", "todo_list", "error"}


def toml(value) -> str:
    """A TOML value for ``-c key=value`` (JSON strings and arrays are valid TOML)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)} = {toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def command(attach: dict, opts: dict, session: str | None = None) -> list[str]:
    n = attach["name"]
    cmd = ["codex", "exec"] + (["resume", session] if session else []) + [
        "--json", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "-m", opts["model"],
    ]
    if not opts.get("resumable"):
        cmd.append("--ephemeral")
    for f in DISABLE:
        cmd += ["--disable", f]
    conf = {
        "sandbox_mode": "read-only",
        "approval_policy": "never",
        "web_search": "disabled",
        "include_environment_context": False,
        "developer_instructions": NEUTRAL_PROMPT,
        f"mcp_servers.{n}.default_tools_approval_mode": "approve",
    }
    if opts.get("effort"):
        conf["model_reasoning_effort"] = opts["effort"]
    if attach["transport"] == "stdio":
        conf[f"mcp_servers.{n}.command"] = attach["command"]
        conf[f"mcp_servers.{n}.args"] = attach["args"]
        conf[f"mcp_servers.{n}.cwd"] = attach["cwd"]
        if attach["env"]:
            conf[f"mcp_servers.{n}.env"] = attach["env"]
    else:
        conf[f"mcp_servers.{n}.url"] = attach["url"]
        if attach.get("token"):
            conf[f"mcp_servers.{n}.bearer_token_env_var"] = TOKEN_ENV
    for k, v in conf.items():
        cmd += ["-c", f"{k}={toml(v)}"]
    return cmd + ["-"]


def _result_text(result) -> str:
    if not isinstance(result, dict):
        return "" if result is None else str(result)
    text = "\n".join(b.get("text", "") for b in result.get("content") or [] if isinstance(b, dict))
    if not text and result.get("structured_content") is not None:
        text = json.dumps(result["structured_content"])
    return text


def parse(lines: list[str], server: str) -> dict:
    ep = empty_episode()
    last_message = ""
    batches, in_batch = 0, False
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("type")
        if kind == "thread.started":
            ep["session"] = ev.get("thread_id")
        elif kind == "item.completed":
            item = ev.get("item") or {}
            t = item.get("type")
            if t == "agent_message":
                last_message, in_batch = item.get("text") or "", False
            elif t == "mcp_tool_call" and item.get("server") == server:
                batches += not in_batch
                in_batch = True
                result = item.get("result")
                is_error = bool(item.get("error")) or item.get("status") == "failed" or bool(
                    isinstance(result, dict) and (result.get("is_error") or result.get("isError")))
                text = _result_text(result) or (item.get("error") or {}).get("message", "")
                data = result.get("structured_content") if isinstance(result, dict) else None
                ep["calls"].append(make_call(item.get("tool", ""), item.get("arguments") or {}, text,
                                             is_error, data))
            elif t == "mcp_tool_call" and item.get("server") == "codex":
                pass  # codex's own resource listing, not a tool of the surface
            elif t not in QUIET_ITEMS:
                ep["offsurface"] += 1
        elif kind == "turn.completed":
            u = ev.get("usage") or {}
            cached = int(u.get("cached_input_tokens") or 0)
            ep["tokens"]["input"] += int(u.get("input_tokens") or 0) - cached
            ep["tokens"]["cache_read"] += cached
            ep["tokens"]["cache_write"] += int(u.get("cache_write_input_tokens") or 0)
            ep["tokens"]["output"] += int(u.get("output_tokens") or 0)
            ep["stop"] = "completed"
        elif kind in ("turn.failed", "error"):
            msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
            ep["error"], ep["stop"] = f"codex: {msg or kind}", "failed"
    ep["turns"] = batches + 1  # an estimate: codex reports no model-turn count
    ep["answers"] = [last_message] if last_message else []
    ep["answer"] = last_message
    if ep["stop"] is None and not ep["error"]:
        ep["error"] = "no turn.completed event"
    return ep


class CodexDriver(Driver):
    name = "codex"
    default_model = "gpt-5.6-luna"

    def turn(self, prompt, attach, opts, cwd, session, transcript) -> dict:
        env = {TOKEN_ENV: attach["token"]} if attach.get("token") else {}
        lines, err, code = run_cli(command(attach, opts, session), prompt, cwd, opts["timeout_s"], env=env)
        for line in lines:
            transcript.write(line + "\n")
        ep = parse(lines, attach["name"])
        if code is None:
            ep["error"], ep["stop"] = f"timed out after {opts['timeout_s']}s", "timeout"
        elif ep["error"] == "no turn.completed event":
            ep["error"] = f"codex exited {code}: {err[-300:]}"
        if ep["offsurface"] and not ep["error"]:
            ep["error"] = f"{ep['offsurface']} calls off the surface"
        return ep

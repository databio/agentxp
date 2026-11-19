"""The MCP surface: start the repo's fixture server, call its tools, describe it to drivers.

The fixture contract: ``server.start`` is a shell command (``{port}`` is replaced
with a free port) that prints ONE JSON line on stdout when it is ready::

    {"url": "http://127.0.0.1:PORT/mcp", "token": "...", "tokens": {"observer": "..."}, "vars": {...}}

``token`` is the default identity's bearer token, ``tokens`` the others (for a
task's ``as:``), ``vars`` the fixture names a task file can use as ``{{vars.x}}``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

RESULT_KEEP = 8192  # characters of each tool result kept in runs.jsonl and shown to the judge


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def error_code(text: str) -> str | None:
    """The ``error`` (or ``code``) field of the first JSON object in an error text."""
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            code = obj.get("error") or obj.get("code")
            if isinstance(code, dict):
                code = code.get("code") or code.get("error")
            return str(code) if code else None
        return None
    return None


def make_call(tool: str, args: dict, text: str, is_error: bool, data: Any = None) -> dict:
    """One tool call, the same shape for golden runs and every driver. ``structured`` is the
    result's structuredContent when the server sent one (``data`` falls back to the text)."""
    structured = data
    if data is None:
        try:
            data = json.loads(text)
        except ValueError:
            data = text
    return {
        "tool": tool,
        "args": args,
        "error": bool(is_error),
        "error_code": error_code(text) if is_error else None,
        "bytes": len(text.encode()),
        "text": text,
        "data": data,
        "structured": structured,
    }


class Fixture:
    """One running instance of the server under test (or a description of a stdio one)."""

    def __init__(self, spec: dict, root: Path, log_dir: Path | None = None):
        self.server = spec["server"]
        self.name = self.server["name"]
        self.root = root
        self.log_dir = log_dir
        self.proc: subprocess.Popen | None = None
        self.ready: dict = {}

    @property
    def kind(self) -> str:
        return "stdio" if self.server.get("stdio") else "http"

    @property
    def vars(self) -> dict:
        return self.ready.get("vars") or {}

    def _env(self) -> dict:
        return {**os.environ, **{k: str(v) for k, v in (self.server.get("env") or {}).items()}}

    def start(self) -> "Fixture":
        s = self.server
        if s.get("url"):
            token = os.environ.get(s["token_env"], "") if s.get("token_env") else ""
            self.ready = {"url": s["url"], "token": token}
            if s.get("vars_cmd"):
                self.ready["vars"] = self._vars_from_cmd()
            return self
        if s.get("stdio"):
            self.ready = {"vars": self._vars_from_cmd()} if s.get("vars_cmd") else {}
            return self
        cmd = s["start"].replace("{port}", str(free_port()))
        log = open(self.log_dir / "server.log", "ab") if self.log_dir else subprocess.DEVNULL
        self.proc = subprocess.Popen(
            cmd, shell=True, cwd=self.root, env=self._env(), stdout=subprocess.PIPE,
            stderr=log, start_new_session=True,
        )
        lines: list[str] = []
        found = threading.Event()

        def read() -> None:
            for raw in self.proc.stdout:  # type: ignore[union-attr]
                line = raw.decode(errors="replace").strip()
                if not found.is_set():
                    lines.append(line)
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and "url" in obj:
                        self.ready = obj
                        found.set()
                if log is not subprocess.DEVNULL:
                    log.write(raw)

        threading.Thread(target=read, daemon=True).start()
        deadline = time.time() + float(s.get("ready_timeout_s", 60))
        if not found.wait(max(0.0, deadline - time.time())):
            self.stop()
            raise RuntimeError(f"fixture server printed no ready line: {cmd}\n" + "\n".join(lines[-5:]))
        url = urlparse(self.ready["url"])
        while True:  # the ready line may come a moment before the port accepts
            try:
                socket.create_connection((url.hostname, url.port or 80), timeout=1).close()
                return self
            except OSError:
                if time.time() > deadline:
                    self.stop()
                    raise RuntimeError(f"fixture server never accepted connections at {url.geturl()}")
                time.sleep(0.1)

    def _vars_from_cmd(self) -> dict:
        out = subprocess.run(self.server["vars_cmd"], shell=True, cwd=self.root, env=self._env(),
                             capture_output=True, text=True, timeout=120)
        for line in out.stdout.splitlines():
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj.get("vars", obj)
        raise RuntimeError(f"vars_cmd printed no JSON: {self.server['vars_cmd']}")

    def reset(self) -> None:
        """For a running server (``url``): run the ``reset`` command before a writing task."""
        cmd = self.server.get("reset")
        if self.server.get("url") and cmd and cmd != "fresh":
            subprocess.run(cmd, shell=True, cwd=self.root, env=self._env(), check=True)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None

    def token(self, identity: str | None = None) -> str:
        idents = self.server.get("identities") or {}
        if identity and identity != idents.get("default", "default"):
            tok = (self.ready.get("tokens") or {}).get(identity)
            if tok is None:
                raise RuntimeError(f"the fixture's ready line has no token for {identity!r}")
            return tok
        return self.ready.get("token") or ""

    def attachment(self, identity: str | None = None) -> dict:
        """What a driver needs to connect a chatbot to this surface."""
        if self.kind == "stdio":
            cmd = [str(x) for x in self.server["stdio"]]
            return {"surface": "mcp", "name": self.name, "transport": "stdio", "command": cmd[0],
                    "args": cmd[1:], "env": {k: str(v) for k, v in (self.server.get("env") or {}).items()},
                    "cwd": str(self.root)}
        return {"surface": "mcp", "name": self.name, "transport": "http", "url": self.ready["url"],
                "token": self.token(identity)}

    # -- tool calls (golden paths and state checks), over the official MCP SDK --

    @asynccontextmanager
    async def _client(self, identity: str | None):
        from mcp import Client, StdioServerParameters, stdio_client

        if self.kind == "stdio":
            cmd = [str(x) for x in self.server["stdio"]]
            extra = {k: str(v) for k, v in (self.server.get("env") or {}).items()}
            params = StdioServerParameters(command=cmd[0], args=cmd[1:], env=extra, cwd=str(self.root))
            log = open(self.log_dir / "server.log", "a") if self.log_dir else open(os.devnull, "w")
            with log:
                async with Client(stdio_client(params, errlog=log)) as c:
                    yield c
            return
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        tok = self.token(identity)
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        async with httpx2.AsyncClient(headers=headers, timeout=120) as http:
            async with Client(streamable_http_client(self.ready["url"], http_client=http)) as c:
                yield c

    def call_steps(self, steps, identity: str | None = None) -> list[dict]:
        """Run ``steps`` (a generator receiving each call and yielding ``(tool, args)``)."""
        async def go() -> list[dict]:
            calls: list[dict] = []
            async with self._client(identity) as c:
                gen = steps()
                try:
                    tool, args = next(gen)
                    while True:
                        calls.append(await _call(c, tool, args))
                        tool, args = gen.send(calls[-1])
                except StopIteration:
                    pass
            return calls

        return asyncio.run(go())

    def call(self, tool: str, args: dict, identity: str | None = None) -> dict:
        def one():
            yield tool, args
        return self.call_steps(one, identity)[0]


async def _call(client, tool: str, args: dict) -> dict:
    try:
        r = await client.call_tool(tool, args)
    except Exception as e:  # a protocol error is a failed call, not a crash
        return make_call(tool, args, json.dumps({"error": "protocol_error", "message": str(e)}), True)
    text = "\n".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
    data = r.structured_content if r.structured_content is not None else None
    return make_call(tool, args, text, r.is_error, data)


def describe(spec: dict) -> str:
    s = spec["server"]
    if s.get("stdio"):
        return "stdio: " + shlex.join(str(x) for x in s["stdio"])
    return s.get("start") or s.get("url")


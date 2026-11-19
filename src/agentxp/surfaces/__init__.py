"""Tool surfaces a chatbot can be tested through.

``mcp`` (an MCP server, stdio or Streamable HTTP) is the only one implemented.
Planned, with the same task file and drivers:

- ``skill``: a SKILL.md plus a Bash tool restricted to one CLI; a "call" is a CLI command.
- ``http``: an OpenAPI description plus an HTTP request tool; a "call" is a request.

A surface starts the thing under test, makes calls for golden paths and state
checks, and hands drivers an ``attachment`` dict (``surface`` says which kind);
each driver turns that into its CLI's own flags.
"""

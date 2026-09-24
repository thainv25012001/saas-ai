"""The MCP server (docs/PHASE-7.md): an agent's read tools, served over
Streamable HTTP at `/mcp` to any client holding one of that agent's API keys.

- `server.py` -- the low-level `mcp` `Server` and its two handlers.
- `auth.py` -- the bearer-token verifier and the route-scoped 401 gate.
- `app.py` -- the ASGI app and session manager `app/main.py` mounts.
"""

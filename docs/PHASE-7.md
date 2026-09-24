# Phase 7 — MCP

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §8 (MCP integration path) and §3.1
> (`api_keys`). That document is the binding spec; this one records what Phase 7 adds and
> where it departs from it.

**Goal:** a business can point its own MCP client — Claude Desktop, Claude Code, an
internal agent — at this product and ask its catalogue and knowledge base questions, using
the **same tools, under the same tenant rules**, as the sales assistant itself.

```text
MCP client ──HTTP POST /mcp──▶ Authorization: Bearer sa_mcp_…
                                   │
                        api key ──▶ (organization, agent)        SECURITY DEFINER lookup
                                   │
          tools/list ──▶ agent's granted tools ∩ MCP-exposed read tools
          tools/call ──▶ tenant_session ─▶ the SAME ToolRegistry the chat turn builds
                                   │
                        CallToolResult(content = ToolResult.content,
                                       structured_content = data + citations,
                                       is_error = ToolResult.is_error)
```

---

## 1. Scope

**In:** an MCP server (Streamable HTTP, stateless, JSON responses) mounted in the API at
`/mcp`; organization API keys bound to one agent, created and revoked from the agent's
page; `search_products`, `get_product` and `retrieve_knowledge` exposed through it;
per-key rate limiting; the tool runtime extracted from `ChatService` so chat and MCP share
one implementation.

**Out:** consuming remote MCP servers as agent tools (ARCHITECTURE.md §8 "Direction 1"),
`create_lead` over MCP, OAuth, MCP resources and prompts, stdio transport. §8 says why.

---

## 2. The decision everything follows from: an API key *is* an agent's grant

ARCHITECTURE.md §8 promised that an MCP caller gets "the identical context the internal
agent uses, so tools cannot behave differently depending on who calls them". The simplest
way to make that literally true is to **bind every API key to one agent**:

- The tools an MCP caller can list and call are exactly the tools that agent is granted in
  `agent_tools` (Phase 4's single source of truth), intersected with the set Phase 7 chooses
  to expose (§5). Turning a tool off on the agent's Tools card turns it off over MCP too.
- `ToolContext.agent_id` is that agent, and `organization_id` is the key's organization —
  both server-derived from the key, never from the request.
- No second permission model (`scopes text[]` in §3.1) exists to drift from the first. ★

The cost: a business that wants a differently-shaped tool set for MCP creates a second
agent for it. That is cheap, and it keeps "what can this key do?" answerable by looking at
one screen.

---

## 3. Schema

```text
api_keys
  id, organization_id, agent_id → agents ON DELETE CASCADE,   ★ bound to an agent (§2)
  name varchar(100),
  key_prefix varchar(16),     -- shown in the dashboard: "sa_mcp_3f9a1c2e…"
  key_hash bytea UNIQUE,      -- sha256 of the full token; the token itself is never stored
  created_by → users ON DELETE SET NULL,
  last_used_at NULL, revoked_at NULL, created_at, updated_at
```

`scopes text[]` from ARCHITECTURE.md §3.1 is dropped (§2). ★

**Token format:** `sa_mcp_` + 43 characters of `secrets.token_urlsafe(32)`. 256 bits of
entropy makes a plain SHA-256 the right hash: password hashing (argon2) exists to slow
guessing of *low*-entropy secrets and would add ~50 ms to every MCP call for no benefit.
The token is shown exactly once, in the create response.

**RLS, and the one query that cannot use it.** `api_keys` is tenant-owned with
`enable_rls` like every other table. But authentication happens *before* the organization
is known — that is what the key is for. So the lookup goes through one function:

```sql
CREATE FUNCTION resolve_api_key(p_hash bytea)
RETURNS TABLE (api_key_id uuid, organization_id uuid, agent_id uuid)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
AS $$ SELECT id, organization_id, agent_id FROM api_keys
      WHERE key_hash = p_hash AND revoked_at IS NULL $$;
REVOKE ALL ON FUNCTION resolve_api_key(bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION resolve_api_key(bytea) TO app_user;
```

It is owned by the migration role, which owns the table and so is not subject to its
(non-`FORCE`d) policy. It returns three ids for one exact hash and nothing else: it cannot
list keys, cannot search, and reveals nothing for a wrong token. Everything after it runs
in an ordinary `tenant_session` for the returned organization.

---

## 4. Transport and authentication

**Streamable HTTP, stateless, JSON responses**, built from `mcp==2.2.x`'s low-level
`Server` and `StreamableHTTPSessionManager`, mounted as the single route `/mcp` inside the
FastAPI app, with the session manager run from FastAPI's own lifespan. Stateless because
the API may run as several replicas and nothing about these tools needs a session;
JSON responses because no tool streams.

**Authentication** is Starlette's `AuthenticationMiddleware` with the SDK's
`BearerAuthBackend` and a `TokenVerifier` of our own that hashes the bearer token and calls
`resolve_api_key`, applied to `/mcp` only. A missing, malformed, unknown or revoked key is
**HTTP 401** before any JSON-RPC is read. A key whose agent is `disabled` is also 401: a
disabled agent must not answer anyone.

**Host-header protection must be explicit.** The SDK only enables DNS-rebinding protection
by default through a convenience wrapper this embedding does not use, so
`TransportSecuritySettings` is always passed, with allowed hosts from a new setting
`MCP_ALLOWED_HOSTS` (default `localhost:*,127.0.0.1:*`). A deployment adds its public
host there; `DEPLOYMENT.md` says so.

**Rate limit:** 120 calls per key per minute, through the existing Redis limiter, counted
on `tools/call` only (listing is free). Exceeding it is a tool error result, not a 5xx, so
a well-behaved client can back off.

`last_used_at` is updated at most once a minute per key, so an active client does not
turn every call into a write.

---

## 5. What is exposed, and what is not

| Tool | Over MCP | Why |
|---|---|---|
| `search_products` | yes | the point of the phase |
| `get_product` | yes | |
| `retrieve_knowledge` | yes | |
| `create_lead` | **no** | A lead belongs to a conversation (`leads.conversation_id NOT NULL`), and an MCP call has none. A write made by an unattended key also needs its own audit trail and abuse story, which the product does not have yet. |

The exposed set is a constant (`MCP_EXPOSED_TOOL_NAMES`) next to the MCP server, and a
test pins that `create_lead` is not in it. A new write tool is therefore unreachable over
MCP until someone adds it there on purpose.

**One tool runtime, two callers.** The code that resolves an agent's granted tools, builds
the registry of only those tools, and runs each call under a savepoint with a
`statement_timeout` (`_resolve_enabled_tool_names`, `_build_registry`,
`_LockedSessionTool` in `app/chat/service.py`) moves to `app/tools/runtime.py`, unchanged
in behaviour, and both `ChatService` and the MCP server call it. Copying it would give MCP
the timeouts and grant checks as they were on the day of the copy.

`ToolContext.conversation_id` becomes optional (`None` over MCP). `create_lead` refuses a
call without one, as a second line behind §5's exclusion.

**Results:** `content` is `ToolResult.content` as text — what our own model reads —
`structured_content` carries `data` and the citations (document, chunk and product ids,
titles, excerpts), and `is_error` is `ToolResult.is_error`. An unknown or ungranted tool
name is a JSON-RPC error (`-32602`), and so is invalid arguments. Any unexpected exception
is caught and returned as a generic error result. The SDK would otherwise send the raw
exception text to the caller.

---

## 6. Dashboard

The agent detail page gets an **MCP access** card:

- the endpoint URL and the tools this agent exposes over MCP;
- create a key (a name) → the token, shown once, with copy buttons for
  `claude mcp add --transport http <name> <url> --header "Authorization: Bearer <token>"`
  and a JSON client config;
- a list of keys: name, prefix, created by, created, last used, **Revoke**.

GraphQL: `apiKeys(agentId)`, `createApiKey(agentId, name) → {apiKey, token}`,
`revokeApiKey(id)`. Creating and revoking keys requires the **owner or admin** role — the
first mutation in the product to check a role, because a key is a credential that outlives
the session of the person who made it. At most 10 active keys per agent.

---

## 7. Observability

Every `tools/call` logs `mcp_tool_call` with `request_id`, `organization_id`, `agent_id`,
`api_key_id`, the tool name, `is_error` and `duration_ms` — never arguments or results,
which can carry customer questions. `tools/list` logs at debug. A rejected key logs
`mcp_auth_rejected` with the reason (`missing`, `malformed`, `unknown_or_revoked`,
`agent_disabled`) and the token prefix only.

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| A leaked key exposes the catalogue and knowledge base | Read-only tools only; revocable from the dashboard, effective on the next request; prefix shown so a leaked key can be identified; rate-limited. |
| The SECURITY DEFINER function becomes a way around RLS | It takes an exact hash and returns three ids; `search_path` pinned; EXECUTE granted to `app_user` only; a test proves it returns nothing for a wrong or revoked hash. |
| DNS rebinding against a local deployment | Explicit `TransportSecuritySettings` with an allow-list (§4). |
| The SDK is a new major version (v2) | Pinned `mcp>=2.2,<3`; the integration tests speak raw JSON-RPC as well as through the SDK client, so an SDK regression shows up as a test failure, not a silent change on the wire. |
| Tool descriptions were written for our own model | They are shown unchanged to external models. They read well enough generically. Tailoring them is a follow-up. |

---

## 9. Not delivered

| Not delivered | Why, and where it goes |
|---|---|
| Consuming remote MCP servers as agent tools (`tools.type = 'mcp'`) | Needs outbound-request controls (SSRF), encrypted per-org credentials, and a timeout story for someone else's server. The tool runtime extracted here is where an `MCPToolAdapter` would plug in. |
| `create_lead` over MCP | §5. |
| OAuth / per-user MCP auth | Keys are per organization and agent. OAuth needs an authorization server this product does not have. |
| Per-key usage metering and a usage view | Calls are logged. `usage_events` records LLM and embedding spend, and MCP calls spend neither directly (retrieval's query embedding is untracked, as in chat). |
| MCP resources, prompts, stdio transport | No use case yet. |

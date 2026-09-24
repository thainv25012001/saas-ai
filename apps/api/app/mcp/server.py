"""The low-level `mcp` `Server` behind `/mcp` (docs/PHASE-7.md §5, §7).

Two handlers, both resolved per request from the authenticated key (the
transport is stateless, so there is no session to carry anything between
requests):

- `tools/list` -- the agent's granted tools, intersected with
  `MCP_EXPOSED_TOOL_NAMES`, each with the same JSON schema chat shows the
  model.
- `tools/call` -- rate-limited per key, then run through the same tool
  runtime a chat turn uses (`app/tools/runtime.py`), never a copy of it.

**No exception may reach the SDK's dispatcher.** Its generic handler would
answer the client with `str(exc)` -- an internal message, possibly carrying
data. So each handler body is wrapped: a deliberate `MCPError` passes
through, anything else is logged (`mcp_handler_failed`) and replaced by a
generic error.
"""

import time
import uuid
from typing import Any

import mcp.types as types
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from starlette.requests import Request

from app.api_keys.service import ResolvedApiKey
from app.core.errors import RateLimitError
from app.core.logging import get_logger, request_id_var
from app.core.rate_limit import enforce_rate_limit
from app.core.tenancy import TenantContext, tenant_session
from app.llm.types import ToolUseBlock
from app.mcp.auth import REQUEST_ID_SCOPE_KEY, ApiKeyAccessToken
from app.tools.base import ToolContext, ToolResult
from app.tools.runtime import (
    build_granted_registry,
    resolve_enabled_tool_names,
    serialize_citations,
)

logger = get_logger(__name__)

#: The only tools MCP can ever reach, whatever an agent is granted
#: (docs/PHASE-7.md §5). `create_lead` is deliberately absent: a lead belongs
#: to a conversation, and an MCP call has none. A new tool is unreachable
#: over MCP until someone adds it here on purpose.
MCP_EXPOSED_TOOL_NAMES: frozenset[str] = frozenset(
    {"search_products", "get_product", "retrieve_knowledge"}
)

#: Per key, per minute, counted on `tools/call` only (listing is free, §4).
MCP_RATE_LIMIT_PER_MINUTE = 120
_RATE_LIMIT_WINDOW_SECONDS = 60

_SERVER_NAME = "ai-sales-agent"
_SERVER_VERSION = "0.1.0"

#: A caller-chosen tool name is echoed (log line, -32602 message) only up
#: to this many characters: nothing bounds it otherwise.
_TOOL_NAME_ECHO_LIMIT = 64

_RATE_LIMITED_MESSAGE = "Rate limit exceeded; retry later."
_GENERIC_CALL_FAILURE = "The tool call failed unexpectedly."
_GENERIC_LIST_FAILURE = "Internal error"


class _Principal:
    """Who is calling, resolved from the authenticated request."""

    __slots__ = ("key", "request_id")

    def __init__(self, key: ResolvedApiKey, request_id: str) -> None:
        self.key = key
        self.request_id = request_id

    @property
    def tenant(self) -> TenantContext:
        return TenantContext(
            organization_id=self.key.organization_id,
            user_id=None,
            role=None,
            request_id=self.request_id,
        )

    def log_fields(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "organization_id": str(self.key.organization_id),
            "agent_id": str(self.key.agent_id),
            "api_key_id": str(self.key.api_key_id),
        }


def _principal(ctx: ServerRequestContext[Any, Any]) -> _Principal:
    """The key `RequireApiKey` authenticated. Unreachable without one --
    the route answers 401 first -- but refused explicitly rather than
    assumed, so a future mount that forgets the auth gate fails closed."""
    request = ctx.request
    if not isinstance(request, Request):
        raise MCPError(types.INVALID_REQUEST, "Authentication required")
    user = request.scope.get("user")
    if not isinstance(user, AuthenticatedUser) or not isinstance(
        user.access_token, ApiKeyAccessToken
    ):
        raise MCPError(types.INVALID_REQUEST, "Authentication required")
    request_id = request.scope.get(REQUEST_ID_SCOPE_KEY) or str(uuid.uuid4())
    return _Principal(user.access_token.resolved, str(request_id))


def _echoable(name: str) -> str:
    return name[:_TOOL_NAME_ECHO_LIMIT]


def _exposed(granted: list[str]) -> list[str]:
    """Granted ∩ exposed, in a stable order."""
    return sorted(name for name in granted if name in MCP_EXPOSED_TOOL_NAMES)


def _to_call_result(result: ToolResult) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.content)],
        structured_content={
            "data": result.data,
            "citations": serialize_citations(result.citations),
        },
        is_error=result.is_error,
    )


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


async def _on_list_tools(
    ctx: ServerRequestContext[Any, Any], _params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    principal = _principal(ctx)
    token = request_id_var.set(principal.request_id)
    try:
        async with tenant_session(principal.tenant) as session:
            names = _exposed(
                await resolve_enabled_tool_names(session, principal.tenant, principal.key.agent_id)
            )
            # The registry's own `specs_for`, so the schema here is exactly
            # the one chat shows the model for the same tool.
            specs = build_granted_registry(session, names).specs_for(names)
        logger.debug("mcp_tools_list", tool_count=len(specs), **principal.log_fields())
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.name, description=spec.description, input_schema=spec.input_schema
                )
                for spec in specs
            ]
        )
    except MCPError:
        raise
    except Exception:
        logger.exception("mcp_handler_failed", method="tools/list", **principal.log_fields())
        raise MCPError(types.INTERNAL_ERROR, _GENERIC_LIST_FAILURE) from None
    finally:
        request_id_var.reset(token)


async def _on_call_tool(
    ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    principal = _principal(ctx)
    token = request_id_var.set(principal.request_id)
    started = time.monotonic()
    is_error = True
    try:
        result = await _call_tool(principal, params)
        is_error = result.is_error
        return result
    except MCPError:
        raise
    except Exception:
        logger.exception(
            "mcp_handler_failed",
            method="tools/call",
            tool_name=_echoable(params.name),
            **principal.log_fields(),
        )
        return _error_result(_GENERIC_CALL_FAILURE)
    finally:
        # Never arguments or results (§7): they can carry customer questions.
        logger.info(
            "mcp_tool_call",
            tool_name=_echoable(params.name),
            is_error=is_error,
            duration_ms=int((time.monotonic() - started) * 1000),
            **principal.log_fields(),
        )
        request_id_var.reset(token)


async def _call_tool(
    principal: _Principal, params: types.CallToolRequestParams
) -> types.CallToolResult:
    try:
        await enforce_rate_limit(
            f"mcp:{principal.key.api_key_id}",
            limit=MCP_RATE_LIMIT_PER_MINUTE,
            window_seconds=_RATE_LIMIT_WINDOW_SECONDS,
        )
    except RateLimitError:
        # A tool error, not a 5xx or protocol error (§4), so a
        # well-behaved client can back off and retry.
        return _error_result(_RATE_LIMITED_MESSAGE)

    tenant = principal.tenant
    async with tenant_session(tenant) as session:
        names = _exposed(await resolve_enabled_tool_names(session, tenant, principal.key.agent_id))
        if params.name not in names:
            # One wording for "exists nowhere", "exists but not granted" and
            # "granted but not exposed" (`create_lead`): a caller learns
            # nothing about what tools exist beyond its own list.
            raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {_echoable(params.name)}")

        registry = build_granted_registry(session, names)
        # Invalid arguments come back from `registry.execute` as its
        # ordinary `is_error=True` result, and are passed on as one -- not
        # turned into a JSON-RPC error -- so the calling model reads what
        # was wrong and can correct itself, exactly as it would in chat.
        result = await registry.execute(
            ToolUseBlock(id=f"mcp-{uuid.uuid4()}", name=params.name, input=params.arguments or {}),
            ToolContext(
                organization_id=tenant.organization_id,
                agent_id=principal.key.agent_id,
                conversation_id=None,
                request_id=principal.request_id,
                visitor_id=None,
            ),
        )
    return _to_call_result(result)


def build_mcp_server() -> Server[Any]:
    return Server(
        _SERVER_NAME,
        version=_SERVER_VERSION,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )

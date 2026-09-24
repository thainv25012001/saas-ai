"""The MCP endpoint (`/mcp`, docs/PHASE-7.md §4, §5, §7), driven in-process.

Every test runs the real FastAPI app -- auth middleware, the SDK's
stateless JSON-response transport, its Host-header check -- through
`httpx.ASGITransport`, with FastAPI's own lifespan entered so the session
manager's task group is running exactly as it would under uvicorn. No
network: `base_url` is `http://localhost:8000` only so the default
`MCP_ALLOWED_HOSTS` (`localhost:*`) admits it; the rejected-Host test sets a
foreign `Host` explicitly.

The raw JSON-RPC shapes are the ones recorded in the Task 3 SDK findings:
stateless mode needs no session id, but every request after `initialize`
carries `Mcp-Protocol-Version`.
"""

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from structlog.testing import capture_logs

from app.agents.service import AgentService
from app.api_keys.service import ApiKeyService
from app.core.tenancy import TenantContext, tenant_session
from app.embeddings.hashing import HashingEmbedder
from app.products.embedding_text import embeddable_text
from app.products.schemas import ProductInput
from app.products.service import ProductService
from tests.conftest import enable_builtin_tool
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

PROTOCOL_VERSION = "2025-06-18"
BASE_URL = "http://localhost:8000"

_embedder = HashingEmbedder()


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def mcp_app() -> AsyncIterator[Any]:
    """A fresh app with its lifespan running -- the session manager's
    `run()` is only entered there, and `ASGITransport` does not run
    lifespans on its own."""
    from app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def mcp_client(mcp_app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=mcp_app), base_url=BASE_URL) as client:
        yield client


def _headers(
    token: str | None, *, protocol_version: str | None = PROTOCOL_VERSION
) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if protocol_version is not None:
        headers["Mcp-Protocol-Version"] = protocol_version
    return headers


async def _rpc(
    client: AsyncClient,
    token: str | None,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    headers = _headers(token)
    if extra_headers:
        headers.update(extra_headers)
    return await client.post("/mcp", json=body, headers=headers)


async def _call(
    client: AsyncClient, token: str, name: str, arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = await _rpc(client, token, "tools/call", {"name": name, "arguments": arguments or {}})
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


async def _list_tool_names(client: AsyncClient, token: str) -> list[str]:
    response = await _rpc(client, token, "tools/list")
    assert response.status_code == 200, response.text
    return [tool["name"] for tool in response.json()["result"]["tools"]]


async def _new_agent_with_key(tenant: TenantContext) -> tuple[uuid.UUID, uuid.UUID, str]:
    """(agent_id, api_key_id, plaintext token)."""
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input("MCP Bot"))
        created = await ApiKeyService(session, tenant).create(agent.id, "test key")
    return agent.id, created.api_key.id, created.token


async def _seed_product(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    external_id: str = "sku-1",
    name: str = "Aurora Sedan",
    slug: str = "aurora-sedan",
    description: str = "A fuel-efficient family hybrid sedan.",
) -> uuid.UUID:
    attributes = {"seats": 5, "fuel": "hybrid"}
    [embedding] = await _embedder.embed([embeddable_text(name, description, attributes)])
    product = await ProductService(session, tenant).create(
        ProductInput(
            external_id=external_id,
            name=name,
            slug=slug,
            description=description,
            price=Decimal("28499.00"),
            currency="USD",
            category="sedan",
            attributes=attributes,
            embedding=embedding,
            embedding_model="hashing",
            is_active=True,
        )
    )
    return product.id


async def _set_agent_tool(
    tenant: TenantContext, agent_id: uuid.UUID, tool_name: str, *, enabled: bool
) -> None:
    async with tenant_session(tenant) as session:
        service = AgentService(session, tenant)
        tools = await service.list_tools(agent_id)
        [tool] = [tool for tool, _enabled in tools if tool.name == tool_name]
        await service.set_tool_enabled(agent_id, tool.id, enabled)


# ---------------------------------------------------------------------------
# Handshake and tools/list
# ---------------------------------------------------------------------------


async def test_initialize_then_list_returns_exactly_the_three_exposed_tools(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)

    init = await mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1"},
            },
        },
        headers=_headers(token, protocol_version=None),
    )
    assert init.status_code == 200, init.text
    assert init.headers["content-type"].startswith("application/json")
    assert init.json()["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in init.json()["result"]["capabilities"]

    notified = await mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_headers(token),
    )
    assert notified.status_code == 202

    response = await _rpc(mcp_client, token, "tools/list", request_id=2)
    assert response.status_code == 200, response.text
    tools = response.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == sorted(
        ["get_product", "retrieve_knowledge", "search_products"]
    )
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["properties"]
    by_name = {tool["name"]: tool for tool in tools}
    assert "product_id" in by_name["get_product"]["inputSchema"]["properties"]
    assert "organization_id" not in str(by_name)


async def test_list_schema_is_the_same_json_schema_chat_shows_the_model(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    from app.tools.products import GetProductArgs, SearchProductsArgs
    from app.tools.retrieve import RetrieveKnowledgeArgs

    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    response = await _rpc(mcp_client, token, "tools/list")
    by_name = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert by_name["get_product"]["inputSchema"] == GetProductArgs.model_json_schema()
    assert by_name["search_products"]["inputSchema"] == SearchProductsArgs.model_json_schema()
    assert by_name["retrieve_knowledge"]["inputSchema"] == RetrieveKnowledgeArgs.model_json_schema()


async def test_disabling_a_tool_on_the_agent_removes_it_from_the_list(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    assert "search_products" in await _list_tool_names(mcp_client, token)

    await _set_agent_tool(tenant_a, agent_id, "search_products", enabled=False)

    assert await _list_tool_names(mcp_client, token) == ["get_product", "retrieve_knowledge"]


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


async def test_search_products_returns_content_structured_citations_and_no_error(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    async with tenant_session(tenant_a) as session:
        product_id = await _seed_product(session, tenant_a)

    payload = await _call(mcp_client, token, "search_products", {"query": "Aurora Sedan"})

    result = payload["result"]
    assert result["isError"] is False
    assert "Aurora Sedan" in result["content"][0]["text"]
    assert result["content"][0]["type"] == "text"
    structured = result["structuredContent"]
    assert structured["citations"][0]["product_id"] == str(product_id)
    assert structured["data"]["products"][0]["name"] == "Aurora Sedan"


async def test_get_product_returns_the_product(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    async with tenant_session(tenant_a) as session:
        product_id = await _seed_product(session, tenant_a)

    payload = await _call(mcp_client, token, "get_product", {"product_id": str(product_id)})

    assert payload["result"]["isError"] is False
    assert "Aurora Sedan" in payload["result"]["content"][0]["text"]
    assert payload["result"]["structuredContent"]["citations"][0]["product_id"] == str(product_id)


async def test_invalid_arguments_are_an_error_result_not_a_protocol_error(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)

    payload = await _call(mcp_client, token, "get_product", {"product_id": "not-a-uuid"})

    assert "error" not in payload
    assert payload["result"]["isError"] is True
    assert "invalid arguments" in payload["result"]["content"][0]["text"]


async def test_granted_but_unexposed_create_lead_is_not_listed_and_cannot_be_called(
    mcp_client: AsyncClient, tenant_a: TenantContext, owner_connection: AsyncConnection
) -> None:
    """Review Focus 2: an operator granting `create_lead` to the agent does
    not make it reachable over MCP -- not listed, a JSON-RPC error when named,
    and nothing written."""
    agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    await enable_builtin_tool(owner_connection, tenant_a, agent_id, "create_lead")

    assert "create_lead" not in await _list_tool_names(mcp_client, token)

    payload = await _call(
        mcp_client,
        token,
        "create_lead",
        {"name": "Mallory", "email": "mallory@example.com", "phone": "555-0100"},
    )
    assert "result" not in payload
    assert payload["error"]["code"] == -32602
    assert payload["error"]["message"] == "Unknown tool: create_lead"

    leads = (
        await owner_connection.execute(
            text("SELECT count(*) FROM leads WHERE organization_id = :org"),
            {"org": tenant_a.organization_id},
        )
    ).scalar_one()
    assert leads == 0


async def test_an_unknown_name_gets_the_same_error_as_an_unexposed_one(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    payload = await _call(mcp_client, token, "drop_all_tables")
    assert payload["error"] == {"code": -32602, "message": "Unknown tool: drop_all_tables"}


async def test_a_disabled_tool_cannot_be_called_by_name(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    await _set_agent_tool(tenant_a, agent_id, "get_product", enabled=False)
    payload = await _call(mcp_client, token, "get_product", {"product_id": str(uuid.uuid4())})
    assert payload["error"]["code"] == -32602


async def test_org_a_key_cannot_read_org_b_product(
    mcp_client: AsyncClient, tenant_a: TenantContext, tenant_b: TenantContext
) -> None:
    """Review Focus 3: the ordinary not-found tool result, nothing of org B's
    in the body."""
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    async with tenant_session(tenant_b) as session:
        foreign_id = await _seed_product(
            session,
            tenant_b,
            name="Borealis Secret Truck",
            slug="borealis-secret-truck",
            description="Org B's confidential product.",
        )

    response = await _rpc(
        mcp_client,
        token,
        "tools/call",
        {"name": "get_product", "arguments": {"product_id": str(foreign_id)}},
    )

    payload = response.json()
    assert payload["result"]["isError"] is True
    assert payload["result"]["content"][0]["text"] == "No product found with that id."
    assert "Borealis" not in response.text
    assert "confidential" not in response.text


# ---------------------------------------------------------------------------
# Authentication (Review Focus 1)
# ---------------------------------------------------------------------------


async def test_missing_bearer_is_401(mcp_client: AsyncClient) -> None:
    with capture_logs() as entries:
        response = await _rpc(mcp_client, None, "tools/list")
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    rejected = [e for e in entries if e["event"] == "mcp_auth_rejected"]
    assert [e["reason"] for e in rejected] == ["missing"]


async def test_garbage_bearer_is_401_and_logged_as_malformed(mcp_client: AsyncClient) -> None:
    with capture_logs() as entries:
        response = await _rpc(mcp_client, "not-a-real-token-at-all", "tools/list")
    assert response.status_code == 401
    rejected = [e for e in entries if e["event"] == "mcp_auth_rejected"]
    assert [e["reason"] for e in rejected] == ["malformed"]
    assert "not-a-real-token-at-all" not in str(entries)


async def test_well_formed_unknown_token_is_401(mcp_client: AsyncClient) -> None:
    from app.api_keys.tokens import generate_token

    with capture_logs() as entries:
        response = await _rpc(mcp_client, generate_token(), "tools/list")
    assert response.status_code == 401
    rejected = [e for e in entries if e["event"] == "mcp_auth_rejected"]
    assert [e["reason"] for e in rejected] == ["unknown_or_revoked"]


async def test_revoked_key_is_401_on_the_very_next_request(
    mcp_client: AsyncClient,
    tenant_a: TenantContext,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _agent_id, key_id, token = await _new_agent_with_key(tenant_a)
    with capture_logs() as entries:
        assert (await _rpc(mcp_client, token, "tools/list")).status_code == 200

        async with tenant_session(tenant_a) as session:
            await ApiKeyService(session, tenant_a).revoke(key_id)

        response = await _rpc(mcp_client, token, "tools/list")

    assert response.status_code == 401
    assert token not in response.text
    rejected = [e for e in entries if e["event"] == "mcp_auth_rejected"]
    assert [e["reason"] for e in rejected] == ["unknown_or_revoked"]
    assert rejected[0]["token_prefix"] == token[:15]
    assert token not in str(entries)
    assert token not in caplog.text


async def test_disabled_agent_is_401(
    mcp_client: AsyncClient, tenant_a: TenantContext, owner_connection: AsyncConnection
) -> None:
    agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    assert (await _rpc(mcp_client, token, "tools/list")).status_code == 200

    await owner_connection.execute(
        text("UPDATE agents SET status = 'disabled' WHERE id = :id"), {"id": agent_id}
    )
    await owner_connection.commit()

    with capture_logs() as entries:
        response = await _rpc(mcp_client, token, "tools/list")
    assert response.status_code == 401
    rejected = [e for e in entries if e["event"] == "mcp_auth_rejected"]
    assert [e["reason"] for e in rejected] == ["agent_disabled"]
    assert token not in str(entries)


async def test_successful_auth_touches_last_used_at(
    mcp_client: AsyncClient, tenant_a: TenantContext, owner_connection: AsyncConnection
) -> None:
    _agent_id, key_id, token = await _new_agent_with_key(tenant_a)
    assert (await _rpc(mcp_client, token, "tools/list")).status_code == 200
    last_used = (
        await owner_connection.execute(
            text("SELECT last_used_at FROM api_keys WHERE id = :id"), {"id": key_id}
        )
    ).scalar_one()
    assert last_used is not None


async def test_the_rest_of_the_api_is_unaffected_by_the_mcp_mount(mcp_client: AsyncClient) -> None:
    response = await mcp_client.get("/health")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Transport security (Review Focus 4)
# ---------------------------------------------------------------------------


# Both eras the SDK routes on: a handshake-era version, and the modern
# per-request-envelope one `mcp.Client`'s default `mode="auto"` speaks.
@pytest.mark.parametrize("protocol_version", [PROTOCOL_VERSION, "2026-07-28"])
async def test_a_foreign_host_header_is_rejected_by_the_transport(
    mcp_client: AsyncClient, tenant_a: TenantContext, protocol_version: str
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    response = await _rpc(
        mcp_client,
        token,
        "tools/list",
        extra_headers={"Host": "evil.example", "Mcp-Protocol-Version": protocol_version},
    )
    assert response.status_code == 421
    assert "tools" not in response.text


# ---------------------------------------------------------------------------
# Rate limit, logging, and the no-uncaught-exception rule
# ---------------------------------------------------------------------------


async def test_calls_past_the_per_key_limit_are_an_error_result(
    mcp_client: AsyncClient, tenant_a: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.server as mcp_server

    monkeypatch.setattr(mcp_server, "MCP_RATE_LIMIT_PER_MINUTE", 2)
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    args = {"product_id": str(uuid.uuid4())}

    first = await _call(mcp_client, token, "get_product", args)
    second = await _call(mcp_client, token, "get_product", args)
    third = await _call(mcp_client, token, "get_product", args)

    assert first["result"]["content"][0]["text"] == "No product found with that id."
    assert second["result"]["content"][0]["text"] == "No product found with that id."
    assert third["result"]["isError"] is True
    assert third["result"]["content"][0]["text"] == "Rate limit exceeded; retry later."
    # Listing is free (§4): still answers after the limit is hit.
    assert await _list_tool_names(mcp_client, token)


async def test_every_call_logs_mcp_tool_call_without_arguments_or_results(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    agent_id, key_id, token = await _new_agent_with_key(tenant_a)
    with capture_logs() as entries:
        await _call(mcp_client, token, "search_products", {"query": "customer secret question"})

    [entry] = [e for e in entries if e["event"] == "mcp_tool_call"]
    assert entry["organization_id"] == str(tenant_a.organization_id)
    assert entry["agent_id"] == str(agent_id)
    assert entry["api_key_id"] == str(key_id)
    assert entry["tool_name"] == "search_products"
    assert entry["is_error"] is False
    assert isinstance(entry["duration_ms"], int)
    assert entry["request_id"]
    assert "customer secret question" not in str(entries)
    assert token not in str(entries)


async def test_the_request_id_header_correlates_the_tool_call_log(
    mcp_client: AsyncClient, tenant_a: TenantContext
) -> None:
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    with capture_logs() as entries:
        await _rpc(
            mcp_client,
            token,
            "tools/call",
            {"name": "get_product", "arguments": {"product_id": str(uuid.uuid4())}},
            extra_headers={"X-Request-ID": "req-mcp-123"},
        )
    [entry] = [e for e in entries if e["event"] == "mcp_tool_call"]
    assert entry["request_id"] == "req-mcp-123"


async def test_an_unexpected_exception_is_a_generic_error_result(
    mcp_client: AsyncClient, tenant_a: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.server as mcp_server

    class _Exploding:
        async def execute(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("internal detail: password=hunter2")

    monkeypatch.setattr(mcp_server, "build_granted_registry", lambda *_a, **_k: _Exploding())
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)

    with capture_logs() as entries:
        response = await _rpc(
            mcp_client, token, "tools/call", {"name": "get_product", "arguments": {}}
        )

    payload = response.json()
    assert payload["result"]["isError"] is True
    assert "hunter2" not in response.text
    assert "internal detail" not in response.text
    assert [e for e in entries if e["event"] == "mcp_handler_failed"]


async def test_an_unexpected_exception_while_listing_is_a_generic_protocol_error(
    mcp_client: AsyncClient, tenant_a: TenantContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.mcp.server as mcp_server

    async def _explode(*_args: object, **_kwargs: object) -> list[str]:
        raise RuntimeError("internal detail: password=hunter2")

    monkeypatch.setattr(mcp_server, "resolve_enabled_tool_names", _explode)
    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)

    with capture_logs() as entries:
        response = await _rpc(mcp_client, token, "tools/list")

    payload = response.json()
    assert "error" in payload
    assert "hunter2" not in response.text
    assert [e for e in entries if e["event"] == "mcp_handler_failed"]


# ---------------------------------------------------------------------------
# A real MCP client, in-process
# ---------------------------------------------------------------------------


# "legacy" is the `initialize` handshake; "auto" (the SDK's default) is
# discovered to the modern 2026-07-28 era, a different server code path.
@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_the_sdk_client_lists_and_calls_through_the_mounted_app(
    mcp_app: Any, tenant_a: TenantContext, mode: str
) -> None:
    """`mcp.Client` over the SDK's own Streamable HTTP client transport,
    pointed at the mounted FastAPI app through `httpx2.ASGITransport` (the
    SDK's HTTP client is `httpx2`, not `httpx`) -- so the whole deployed path,
    auth middleware included, is exercised by a real client, still with no
    socket."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    _agent_id, _key_id, token = await _new_agent_with_key(tenant_a)
    async with tenant_session(tenant_a) as session:
        product_id = await _seed_product(session, tenant_a)

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=mcp_app),
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:
        transport = streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client)
        async with Client(transport, mode=mode) as client:
            listed = await client.list_tools()
            assert sorted(tool.name for tool in listed.tools) == [
                "get_product",
                "retrieve_knowledge",
                "search_products",
            ]
            result = await client.call_tool("get_product", {"product_id": str(product_id)})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["citations"][0]["product_id"] == str(product_id)

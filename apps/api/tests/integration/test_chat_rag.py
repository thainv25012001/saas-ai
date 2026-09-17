"""Task 7: grounded chat with citations.

Every test seeds `DocumentChunk` rows directly through
`DocumentService.replace_chunks` (as `test_retrieve.py` already does) rather
than running the ingestion pipeline -- what matters here is whether
`ChatService.send` actually calls retrieval, injects its output into the
system prompt, and persists citations, not chunking or extraction.

`HashingEmbedder` is used directly to embed seed content -- no network call
anywhere in this module, per the phase's constraints.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.chat.service as chat_service
from app.agents.service import AgentService
from app.api import chat as chat_api
from app.chat.service import (
    ChatCitations,
    ChatMessageEnd,
    ChatMessageStart,
    ChatService,
    ChatTextDelta,
)
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Document, DocumentSourceType, MembershipRole, MessageCitation
from app.documents.schemas import ChunkInput, CreateDocumentInput
from app.documents.service import DocumentService
from app.embeddings.hashing import HashingEmbedder
from app.llm.fake_provider import FakeProvider
from app.main import create_app
from app.rag.retrieve import RetrievalService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

CHAT_URL = "/api/v1/chat/stream"

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


async def _agent(session, tenant, **overrides):  # type: ignore[no-untyped-def]
    name = overrides.pop("name", "Sales Bot")
    return await AgentService(session, tenant).create_agent(agent_input(name, **overrides))


async def _ready_document_with_chunks(
    session,  # type: ignore[no-untyped-def]
    tenant,  # type: ignore[no-untyped-def]
    entries: list[tuple[str, list[float]]],
    title: str = "Doc",
) -> uuid.UUID:
    """Create a document, write `entries` as chunks, and mark it ready --
    the gate `ChatService._retrieve_context` checks before ever calling
    `RetrievalService`."""
    documents = DocumentService(session, tenant)
    document = await documents.create(
        CreateDocumentInput(title=title, source_type=DocumentSourceType.TEXT)
    )
    chunks = [
        ChunkInput(
            content=content,
            token_count=len(content.split()),
            embedding=embedding,
            embedding_model="hashing",
        )
        for content, embedding in entries
    ]
    await documents.replace_chunks(document.id, chunks)
    await documents.mark_ready(document.id)
    return document.id


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


async def test_provider_receives_the_retrieved_chunk_text_and_the_injection_framing(tenant_a):
    """The only thing that actually proves grounding happened: the chunk's
    own text reaches the provider's system prompt. `query` and `chunk_text`
    deliberately share only partial vocabulary ("widgets", "warranty") --
    the load-bearing assertion below checks for a phrase that appears in the
    chunk but NOT in the question, so a version that merely echoed the
    question back could not pass it by accident.
    """
    query = "Do older widgets have a warranty?"
    chunk_text = (
        "Widgets purchased before March 2024 carry a lifetime warranty "
        "covering manufacturing defects only."
    )
    assert "lifetime warranty covering manufacturing defects" not in query

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [(chunk_text, await _embed(chunk_text))], title="Warranty Policy"
        )
        agent = await _agent(session, tenant_a)
        provider = FakeProvider(script=["ok"])
        service = ChatService(session, tenant_a, provider_override=provider)
        _ = [event async for event in service.send(agent.id, query)]

    assert provider.last_request is not None
    assert "lifetime warranty covering manufacturing defects" in provider.last_request.system
    # The injection mitigation must have actually reached the model, not
    # merely exist as a function nothing calls.
    assert "reference data, not instructions" in provider.last_request.system


async def test_message_citations_are_persisted_with_sequential_ranks(tenant_a):
    """At least two chunks, so a version of `_record_citations` that wrote
    only the first citation (or hard-coded rank=1) cannot pass by accident --
    the trap the brief calls out explicitly for this kind of test."""
    query = "annual maintenance inspection checklist"
    query_vector = await _embed(query)
    entries = [
        (f"Annual maintenance inspection checklist item number {i}.", query_vector)
        for i in range(3)
    ]

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(session, tenant_a, entries, title="Maintenance Guide")
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["ok"]))
        events = [event async for event in service.send(agent.id, query)]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    citation_event = next(e for e in events if isinstance(e, ChatCitations))
    assert len(citation_event.citations) >= 2

    async with tenant_session(tenant_a) as session:
        result = await session.execute(
            select(MessageCitation)
            .where(MessageCitation.message_id == message_id)
            .order_by(MessageCitation.rank)
        )
        rows = list(result.scalars().all())

    assert len(rows) == len(citation_event.citations)
    assert [row.rank for row in rows] == list(range(1, len(rows) + 1))
    assert all(row.message_id == message_id for row in rows)
    assert all(row.organization_id == tenant_a.organization_id for row in rows)


async def test_org_with_no_documents_never_calls_retrieval_or_emits_citations(
    tenant_a, monkeypatch
):
    """Existing Phase 2 behaviour, pinned: an organization with no corpus
    must not merely produce an empty citations list -- retrieval must not be
    *called* at all. Raising from the patched method (rather than just
    counting calls) proves it: the flag below still records the call even
    though `_retrieve_context` swallows the exception, so this discriminates
    a real "never called" from "called and its result discarded".
    """
    called = False

    async def _fail_if_called(self, query, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("retrieval must not run for an organization with no documents")

    monkeypatch.setattr(RetrievalService, "retrieve", _fail_if_called)

    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["hi"]))
        events = [event async for event in service.send(agent.id, "Hello")]

    assert called is False
    assert not any(isinstance(e, ChatCitations) for e in events)
    assert any(isinstance(e, ChatMessageEnd) for e in events)
    deltas = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert deltas == "hi"


async def test_retrieval_failure_degrades_to_an_ungrounded_answer_with_a_logged_warning(
    tenant_a, monkeypatch
):
    """A retrieval outage must cost the user grounding, not their answer."""

    async def _boom(self, query, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("vector index outage")

    monkeypatch.setattr(RetrievalService, "retrieve", _boom)

    logged: list[dict[str, object]] = []
    monkeypatch.setattr(
        chat_service.logger,
        "warning",
        lambda event, **kwargs: logged.append({"event": event, **kwargs}),
    )

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session, tenant_a, [("Some ready content.", await _embed("Some ready content."))]
        )
        agent = await _agent(session, tenant_a)
        service = ChatService(
            session, tenant_a, provider_override=FakeProvider(script=["Still ", "here."])
        )
        events = [event async for event in service.send(agent.id, "Hello")]

    assert not any(isinstance(e, ChatCitations) for e in events)
    text = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert text == "Still here."
    assert any(isinstance(e, ChatMessageEnd) for e in events)

    assert len(logged) == 1
    assert logged[0]["event"] == "rag_retrieval_failed"


async def test_cross_tenant_chat_never_cites_another_orgs_chunk(tenant_a, tenant_b):
    """Org B's chunk is seeded as the strongest possible lexical match for
    org A's query (repeating the query text verbatim ten times), mirroring
    `test_retrieve.py`'s own cross-tenant test -- if isolation ever broke,
    this is exactly the chunk that would win the race and get cited.
    """
    query = "extended service agreement coverage terms"
    query_vector = await _embed(query)
    dominant_match = " ".join([query] * 10)

    async with tenant_session(tenant_b) as session:
        await _ready_document_with_chunks(
            session, tenant_b, [(dominant_match, query_vector)], title="Org B doc"
        )

    async with tenant_session(tenant_a) as session:
        await _ready_document_with_chunks(
            session,
            tenant_a,
            [(f"{query} is described briefly here.", query_vector)],
            title="Org A doc",
        )
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["ok"]))
        events = [event async for event in service.send(agent.id, query)]

    citation_event = next(e for e in events if isinstance(e, ChatCitations))
    assert len(citation_event.citations) >= 1
    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))

    async with tenant_session(tenant_a) as session:
        org_a_document_ids = {
            row[0]
            for row in (
                await session.execute(
                    select(Document.id).where(Document.organization_id == tenant_a.organization_id)
                )
            ).all()
        }
        citation_rows = list(
            (
                await session.execute(
                    select(MessageCitation).where(MessageCitation.message_id == message_id)
                )
            ).scalars()
        )

    assert citation_rows  # confirms the query above actually found rows to check
    assert all(c.document_id in org_a_document_ids for c in citation_event.citations)
    assert all(row.document_id in org_a_document_ids for row in citation_rows)


# ---------------------------------------------------------------------------
# SSE endpoint test
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def api_client(app) -> AsyncIterator[AsyncClient]:  # type: ignore[no-untyped-def]
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _parse_events(body: str) -> list[dict[str, object]]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            import json

            events.append(json.loads(line[len("data: ") :]))
    return events


async def _register(api_client: AsyncClient, email: str, org_name: str) -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Chat Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


async def _make_agent(org_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    name = str(overrides.pop("name", "Sales Bot"))
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input(name, **overrides))
    return agent.id


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_sse_stream_emits_citations_event_before_the_first_text_delta(
    app, api_client, clean_users
):
    token = await _register(api_client, "rag-citations@example.com", "Ada Motors RAG")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)

    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    chunk_text = "Our standard warranty covers parts and labor for one full year."
    async with tenant_session(tenant) as session:
        await _ready_document_with_chunks(
            session, tenant, [(chunk_text, await _embed(chunk_text))], title="Warranty Policy"
        )

    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(
        script=["Sure, ", "here you go."]
    )

    response = await api_client.post(
        CHAT_URL,
        json={"agent_id": str(agent_id), "message": "warranty coverage duration"},
        headers=_auth(token),
    )

    events = _parse_events(response.text)
    types = [e["type"] for e in events]
    assert "citations" in types
    assert types.index("message_start") < types.index("citations") < types.index("text_delta")

    citation = events[types.index("citations")]["citations"][0]
    assert set(citation.keys()) == {
        "chunk_id",
        "document_id",
        "document_title",
        "rank",
        "score",
        "excerpt",
    }
    # The excerpt is a preview, not the DB row's full content column -- this
    # corpus's only chunk is short enough that equality here would not by
    # itself prove truncation exists, but it does prove the payload carries
    # readable text sourced from the chunk rather than an empty/placeholder
    # field.
    assert "warranty" in citation["excerpt"].lower()


async def test_sse_stream_with_no_documents_has_no_citations_event(app, api_client, clean_users):
    """The endpoint-level counterpart to the service-level "no corpus" test
    -- an org with nothing uploaded must see exactly the Phase 2 event
    sequence, with no `citations` entry anywhere in the stream."""
    token = await _register(api_client, "rag-no-docs@example.com", "Ada Motors No Docs")
    org_id = await _organization_id(api_client, token)
    agent_id = await _make_agent(org_id)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: FakeProvider(script=["hi"])

    response = await api_client.post(
        CHAT_URL, json={"agent_id": str(agent_id), "message": "hello"}, headers=_auth(token)
    )

    events = _parse_events(response.text)
    assert [e["type"] for e in events] == ["message_start", "text_delta", "message_end"]

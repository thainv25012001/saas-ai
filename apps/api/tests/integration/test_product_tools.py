"""Task 5: `search_products`/`get_product` (`app/tools/products.py`), and
the end-to-end proof the brief names as this task's actual definition of
done (Ruling 2): an agent created through `AgentService.create_agent` and
driven through `ChatService` reaches the provider with these tools
OFFERED, and the tool's STRUCTURED result appears in the provider's NEXT
request -- asserted on the captured `CompletionRequest` itself, never on
`tools`/`agent_tools` row counts. Phase 4 shipped reviewed, tested, correct
tool code that no agent could ever reach; row counts passed the whole time
that was true, because the miss was in resolution, not in the tool. This
module's central tests are built specifically so that mistake, reproduced
here, would fail them.

Every chat turn below scripts `FakeProvider` with `turns=`, matching
`tests/integration/test_chat_tools.py`'s own note: a loop that runs "until
the model stops asking for tools" cannot be exercised by a fake that can
never ask for one. Since Task 5, `_agent()`/`AgentService.create_agent`
links every new agent to BOTH product tools by default (Ruling 1 -- neither
writes anything, so neither carries the risk that keeps `create_lead`
off) -- so a test below that never calls `enable_builtin_tool` is not
missing a setup step, it is exercising exactly that default.

Product rows are seeded through `ProductService`, embedded with a real
`HashingEmbedder` over their own `embeddable_text`, matching
`tests/integration/test_product_search.py`'s discipline: a semantic search
for a product's own name/description must genuinely find it via the real
embedder, not via a synthetic vector nothing but the test itself would
produce.
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.service import AgentService
from app.chat.service import ChatMessageStart, ChatService, ChatToolCallEnd
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MessageCitation
from app.embeddings.hashing import HashingEmbedder
from app.llm.fake_provider import FakeProvider, FakeToolCall
from app.llm.types import ToolResultBlock
from app.products.embedding_text import embeddable_text
from app.products.schemas import ProductInput
from app.products.service import ProductService
from app.tools.base import ToolContext
from app.tools.products import (
    _AMBIGUOUS_MATCH_QUALITY_NOTE,
    _FLAT_BAND_NOTE,
    _NO_RESULTS_MESSAGE,
    _NOT_FOUND_MESSAGE,
    _SHARP_LEADER_NOTE,
    GetProductArgs,
    GetProductTool,
    SearchProductsArgs,
    SearchProductsTool,
)
from app.tools.registry import ToolRegistry
from tests.factories import agent_input

pytestmark = pytest.mark.anyio

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


async def _agent(session: AsyncSession, tenant: TenantContext, **overrides: object):  # type: ignore[no-untyped-def]
    name = overrides.pop("name", "Sales Bot")
    return await AgentService(session, tenant).create_agent(agent_input(name, **overrides))


def _ctx(tenant: TenantContext, **overrides: object) -> ToolContext:
    payload: dict[str, object] = {
        "organization_id": tenant.organization_id,
        "agent_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "request_id": "req-1",
    }
    payload.update(overrides)
    return ToolContext(**payload)


async def _seed_product(
    session: AsyncSession,
    tenant: TenantContext,
    *,
    external_id: str = "sku-1",
    name: str = "Aurora Sedan",
    slug: str = "aurora-sedan",
    description: str | None = "A fuel-efficient family hybrid sedan.",
    price: Decimal | None = Decimal("28499.00"),
    currency: str | None = "USD",
    category: str | None = "sedan",
    attributes: dict[str, Any] | None = None,
    embed: bool = True,
) -> uuid.UUID:
    """A product with a real `HashingEmbedder` embedding of its own
    `embeddable_text` -- the exact text a real import would embed -- so a
    semantic search for its own name/description genuinely finds it."""
    attrs = attributes if attributes is not None else {"seats": 5, "fuel": "hybrid"}
    embedding = await _embed(embeddable_text(name, description, attrs)) if embed else None
    data = ProductInput(
        external_id=external_id,
        name=name,
        slug=slug,
        description=description,
        price=price,
        currency=currency,
        category=category,
        attributes=attrs,
        embedding=embedding,
        embedding_model="hashing" if embed else None,
    )
    product = await ProductService(session, tenant).create(data)
    return product.id


# ---------------------------------------------------------------------------
# Registration: neither args model can carry organization_id (registry-level
# guarantee, exercised directly rather than only as a side effect of a
# chat turn elsewhere).
# ---------------------------------------------------------------------------


async def test_both_product_tools_register_without_leaking_organization_id() -> None:
    registry = ToolRegistry()
    registry.register(SearchProductsTool(session=None))  # type: ignore[arg-type]
    registry.register(GetProductTool(session=None))  # type: ignore[arg-type]
    specs = registry.specs_for(["search_products", "get_product"])
    assert {spec.name for spec in specs} == {"search_products", "get_product"}


# ---------------------------------------------------------------------------
# Ruling 2's own test: a FRESH agent, created the normal way, reaches the
# provider with both product tools offered -- asserted on the captured
# CompletionRequest, never a row count.
# ---------------------------------------------------------------------------


async def test_a_fresh_agent_reaches_the_provider_with_both_product_tools_offered(tenant_a):
    """No `enable_builtin_tool` fixture call anywhere in this test: Task 5's
    Ruling 1 means `AgentService.create_agent` alone must already grant
    both tools. This is the shape of test the brief calls the actual
    definition of done -- Phase 4's own miss was invisible to a test that
    only checked `tools`/`agent_tools` row counts, since the rows existed
    and the resolution path was still broken.
    """
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id

    provider = FakeProvider(turns=["Hi there! How can I help?"])
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "hello")]

    assert any(getattr(e, "text", None) for e in events)
    assert provider.last_request is not None
    assert provider.last_request.tools is not None
    names = {t.name for t in provider.last_request.tools}
    assert {"search_products", "get_product", "retrieve_knowledge"} <= names


# ---------------------------------------------------------------------------
# The central end-to-end claim: a product question triggers search_products,
# and the STRUCTURED result -- not a paraphrase of it -- reaches the
# provider's next request.
# ---------------------------------------------------------------------------


async def test_product_question_triggers_search_and_structured_result_reaches_next_request(
    tenant_a,
):
    async with tenant_session(tenant_a) as session:
        product_id = await _seed_product(session, tenant_a)
        agent = await _agent(session, tenant_a)
        agent_id = agent.id

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="search_products", input={"query": "hybrid sedan"})],
            "The Aurora Sedan is $28,499.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [
            event async for event in service.send(agent_id, "What hybrid sedans do you sell?")
        ]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is False, tool_end.results[0].result

    # `FakeProvider.last_request` is overwritten on every call -- after a
    # two-step turn, this IS the second request, built from history that
    # now includes the tool's own result. Asserted on that captured
    # `CompletionRequest`, exactly as the brief requires, not on the event
    # stream or a row count.
    assert provider.last_request is not None
    tool_result_blocks = [
        block
        for message in provider.last_request.messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert len(tool_result_blocks) == 1
    result_block = tool_result_blocks[0]
    assert result_block.is_error is False
    # Structured fields the model can read directly -- not prose it has to
    # parse a price out of -- and a `source` naming the exact row
    # (docs/PHASE-5.md §2 item 1).
    assert f"source: product:{product_id}" in result_block.content
    assert "Aurora Sedan" in result_block.content
    assert "28499.00" in result_block.content
    assert "in_stock" in result_block.content


# ---------------------------------------------------------------------------
# The explicit "nothing matched" message -- reachable today through
# filters, per this task's own design discussion.
# ---------------------------------------------------------------------------


async def test_search_products_with_no_filter_matches_returns_an_explicit_message(tenant_a):
    async with tenant_session(tenant_a) as session:
        await _seed_product(session, tenant_a)  # priced far under the filter below
        agent = await _agent(session, tenant_a)
        agent_id = agent.id

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="search_products", input={"min_price": "1000000.00"})],
            "Nothing in that range.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "anything over a million?")]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is False
    assert tool_end.results[0].result == _NO_RESULTS_MESSAGE
    assert tool_end.results[0].result != ""


async def test_direct_call_no_matches_is_an_explicit_message_not_an_empty_list(tenant_a):
    async with tenant_session(tenant_a) as session:
        await _seed_product(session, tenant_a, price=Decimal("100.00"))
        tool = SearchProductsTool(session, embedder=_embedder)
        result = await tool.execute(
            SearchProductsArgs(min_price=Decimal("1000000.00")), _ctx(tenant_a)
        )
    assert result.is_error is False
    assert result.content == _NO_RESULTS_MESSAGE
    assert result.citations == []
    assert result.data is None


# ---------------------------------------------------------------------------
# get_product: the not-found message, and cross-tenant leakage.
# ---------------------------------------------------------------------------


async def test_get_product_for_a_genuinely_missing_id_is_an_error_result(tenant_a):
    async with tenant_session(tenant_a) as session:
        tool = GetProductTool(session)
        result = await tool.execute(GetProductArgs(product_id=uuid.uuid4()), _ctx(tenant_a))
    assert result.is_error is True
    assert result.content == _NOT_FOUND_MESSAGE
    assert result.citations == []


async def test_get_product_for_another_orgs_id_leaks_nothing(tenant_a, tenant_b):
    """The same `NotFoundError`, the same `ToolResult`, whether the id is
    genuinely missing or belongs to another organization -- proven by
    literal equality against the missing-id case, not merely by both being
    `is_error=True`. `ProductService.get`'s own docstring is explicit that
    cross-tenant lookups fail identically to a nonexistent id; this pins
    that guarantee at the tool boundary specifically.
    """
    async with tenant_session(tenant_b) as session:
        foreign_id = await _seed_product(
            session, tenant_b, external_id="sku-yacht", name="Secret Yacht", slug="secret-yacht"
        )

    async with tenant_session(tenant_a) as session:
        tool = GetProductTool(session)
        foreign_result = await tool.execute(GetProductArgs(product_id=foreign_id), _ctx(tenant_a))
        missing_result = await tool.execute(GetProductArgs(product_id=uuid.uuid4()), _ctx(tenant_a))

    assert foreign_result.is_error is True
    assert foreign_result.content == _NOT_FOUND_MESSAGE
    assert foreign_result == missing_result
    assert "Secret Yacht" not in foreign_result.content
    assert foreign_result.data is None


async def test_get_product_end_to_end_for_another_orgs_id_returns_the_error_result(
    tenant_a, tenant_b
):
    """The same guarantee, through a real chat turn: a model that names
    another organization's product id gets the identical error result a
    genuinely missing id would produce, and the leads/products tables never
    prove anything different happened."""
    async with tenant_session(tenant_b) as session:
        foreign_id = await _seed_product(
            session, tenant_b, external_id="sku-yacht-2", name="Hidden Yacht", slug="hidden-yacht"
        )

    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        agent_id = agent.id

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="get_product", input={"product_id": str(foreign_id)})],
            "I couldn't find that.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "tell me about that product")]

    tool_end = next(e for e in events if isinstance(e, ChatToolCallEnd))
    assert tool_end.results[0].is_error is True
    assert tool_end.results[0].result == _NOT_FOUND_MESSAGE
    assert "Hidden Yacht" not in tool_end.results[0].result


# ---------------------------------------------------------------------------
# docs/ARCHITECTURE.md §5.4 rule 3: message_citations records what was
# actually retrieved -- now true for products, not just chunks.
# ---------------------------------------------------------------------------


async def test_message_citations_records_product_id_for_a_search_result(tenant_a):
    async with tenant_session(tenant_a) as session:
        product_id = await _seed_product(session, tenant_a)
        agent = await _agent(session, tenant_a)
        agent_id = agent.id

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="search_products", input={"query": "hybrid sedan"})],
            "Here's the Aurora Sedan.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [
            event async for event in service.send(agent_id, "What hybrid sedans do you sell?")
        ]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        rows = (
            (
                await session.execute(
                    select(MessageCitation).where(MessageCitation.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].product_id == product_id
    assert rows[0].chunk_id is None
    assert rows[0].document_id is None


async def test_get_product_also_records_a_product_citation(tenant_a):
    """A product `get_product` fetched reaches the prompt exactly as surely
    as one `search_products` ranked -- so it must be citable after the
    fact too, not only the tool the brief happens to name first."""
    async with tenant_session(tenant_a) as session:
        product_id = await _seed_product(session, tenant_a)
        agent = await _agent(session, tenant_a)
        agent_id = agent.id

    provider = FakeProvider(
        turns=[
            [FakeToolCall(id="call_1", name="get_product", input={"product_id": str(product_id)})],
            "Yes, it's in stock.",
        ]
    )
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent_id, "is it in stock?")]

    message_id = next(e.message_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        rows = (
            (
                await session.execute(
                    select(MessageCitation).where(MessageCitation.message_id == message_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].product_id == product_id


# ---------------------------------------------------------------------------
# The no-relevance-floor decision (task-5-brief.md, extended in the fix
# round): surface match quality honestly instead of filtering on an
# uncalibratable number -- and key the note on the SHAPE of the returned
# set (self-referential, no cross-embedder calibration) rather than a
# constant string that fires identically on an excellent match and on
# garbage, which the fix round's review correctly identified as carrying
# no information at all.
#
# `_classify_distance_shape`'s own boundary correctness (sharp/flat/the
# reviewer's leading-group regression example) is pinned directly against
# literal distance lists in `tests/unit/test_product_shape_note.py`, not
# here: `tests/integration`'s autouse `_dispose_shared_engine` fixture
# (`tests/integration/conftest.py`) requires an event loop, and a pure
# classification test over plain floats has no database and no loop to
# give it. What remains here is the wiring, end to end, through a real
# `HashingEmbedder` corpus and the real tool.
# ---------------------------------------------------------------------------

_SHAPE_QUERY = "hybrid sedan family safety"


async def _seed_two_close_matches_and_a_tail(session: AsyncSession, tenant: TenantContext) -> None:
    """Two genuinely close matches to `_SHAPE_QUERY` (measured cosine
    distance 0.25 and 0.3118 -- real embeddings, not a manufactured tie)
    above four distractors that each share exactly one incidental query
    word (measured 0.8664-0.8709) -- the real, HashingEmbedder-computed
    version of `docs/PHASE-5.md` §3's "Camry LE vs Camry SE": a leading
    GROUP of two, not a single item, sitting above a real tail. `_seed_product`'s
    own default `attributes` include the word "hybrid", so every product
    here sets `attributes={}` to keep the vocabulary exactly what each
    product's own name/description says."""
    await _seed_product(
        session,
        tenant,
        external_id="near-1",
        name="Aurora Hybrid Sedan",
        slug="aurora-hybrid-sedan",
        description="A safe family hybrid sedan with excellent safety ratings.",
        attributes={},
    )
    await _seed_product(
        session,
        tenant,
        external_id="near-2",
        name="Aurora Hybrid Sedan Plus",
        slug="aurora-hybrid-sedan-plus",
        description="A very safe family hybrid sedan trim with excellent safety ratings.",
        attributes={},
    )
    distractors = [
        (
            "dist-1",
            "Bluetooth Speaker",
            "Waterproof outdoor speaker with deep bass output for the whole family.",
        ),
        (
            "dist-2",
            "Garden Hose",
            "Fifty foot expandable garden safety hose for watering plants.",
        ),
        (
            "dist-3",
            "Office Chair",
            "Ergonomic mesh office sedan-style chair with lumbar support.",
        ),
        (
            "dist-4",
            "Coffee Maker",
            "Programmable drip hybrid coffee maker with thermal carafe.",
        ),
    ]
    for external_id, name, description in distractors:
        await _seed_product(
            session,
            tenant,
            external_id=external_id,
            name=name,
            slug=name.lower().replace(" ", "-"),
            description=description,
            price=Decimal("50.00"),
            category="misc",
            attributes={},
        )


async def test_too_few_results_falls_back_to_the_ambiguous_note(tenant_a):
    """A single result has no shape at all -- not "flat", not "sharp", just
    not enough points to say anything about the SET's shape. Must degrade
    to the original, uniform note rather than assert a confidence this
    data cannot support."""
    async with tenant_session(tenant_a) as session:
        await _seed_product(session, tenant_a)
        tool = SearchProductsTool(session, embedder=_embedder)
        result = await tool.execute(SearchProductsArgs(query="hybrid sedan"), _ctx(tenant_a))
    assert result.is_error is False
    assert _AMBIGUOUS_MATCH_QUALITY_NOTE in result.content
    assert _SHARP_LEADER_NOTE not in result.content
    assert _FLAT_BAND_NOTE not in result.content
    assert "vector_distance" in result.data["products"][0]
    assert "keyword_rank" in result.data["products"][0]


async def test_a_real_leading_group_of_two_gets_the_sharp_leader_note_end_to_end(tenant_a):
    """The real-embeddings version of the reviewer's regression example:
    two genuinely close matches (measured 0.25, 0.3118) above a real tail
    (measured 0.8664-0.8709) -- through the actual `ProductSearchService`
    -> `SearchProductsTool` path, not a literal-number unit test. Fix round
    1's algorithm (rank 1 vs rank 2 only) would have called this "flat":
    the gap between the two leaders (0.3118-0.25=0.0618) is small next to
    the big gap that actually separates the GROUP from the tail
    (0.8664-0.3118=0.5546) -- exactly the shape that made a leading-group
    comparison necessary instead of a leading-item one."""
    async with tenant_session(tenant_a) as session:
        await _seed_two_close_matches_and_a_tail(session, tenant_a)
        tool = SearchProductsTool(session, embedder=_embedder)
        result = await tool.execute(SearchProductsArgs(query=_SHAPE_QUERY), _ctx(tenant_a))

    assert result.is_error is False
    assert _SHARP_LEADER_NOTE in result.content
    assert _FLAT_BAND_NOTE not in result.content
    assert _AMBIGUOUS_MATCH_QUALITY_NOTE not in result.content


async def test_filter_only_browse_carries_no_match_quality_note(tenant_a):
    """A filters-only browse ("everything under £30,000") has no relevance
    question to caveat -- every surviving row satisfied an exact predicate,
    not a similarity score -- so none of the three notes, which exist
    specifically to characterise a RANKED result, must appear here."""
    async with tenant_session(tenant_a) as session:
        await _seed_product(session, tenant_a)
        tool = SearchProductsTool(session, embedder=_embedder)
        result = await tool.execute(
            SearchProductsArgs(max_price=Decimal("100000.00")), _ctx(tenant_a)
        )
    assert result.is_error is False
    for note in (_AMBIGUOUS_MATCH_QUALITY_NOTE, _SHARP_LEADER_NOTE, _FLAT_BAND_NOTE):
        assert note not in result.content
    assert result.data["products"][0]["vector_distance"] is None
    assert result.data["products"][0]["keyword_rank"] is None


# ---------------------------------------------------------------------------
# Cross-tenant search: org B's product must never surface in org A's search.
# ---------------------------------------------------------------------------


async def test_cross_tenant_search_never_returns_another_orgs_product(tenant_a, tenant_b):
    async with tenant_session(tenant_b) as session:
        # Deliberately identical name/description to org A's own product
        # below -- the strongest possible false positive if tenancy ever
        # leaked through the vector or keyword arm.
        foreign_id = await _seed_product(
            session,
            tenant_b,
            external_id="sku-b",
            name="Aurora Sedan",
            slug="aurora-sedan-b",
            description="A fuel-efficient family hybrid sedan.",
        )

    async with tenant_session(tenant_a) as session:
        own_id = await _seed_product(session, tenant_a, external_id="sku-a")

    async with tenant_session(tenant_a) as session:
        tool = SearchProductsTool(session, embedder=_embedder)
        result = await tool.execute(SearchProductsArgs(query="hybrid sedan"), _ctx(tenant_a))

    assert result.is_error is False
    assert result.data is not None
    ids = {row["product_id"] for row in result.data["products"]}
    assert str(own_id) in ids  # the query genuinely matched org A's own product
    assert str(foreign_id) not in ids

"""`search_products`/`get_product`: Task 5's pair, and the reason Phase 5 is
reachable at all (`docs/PHASE-5.md` §2). Without these, "what does the
Camry LE cost" can only ever be answered from a retrieved document chunk --
prose that happens to mention a price -- because there is no other source
in the system. This module does not make that violation impossible (see
this module's own note on the no-floor decision below, and §2 for the
larger argument that Phase 5 does not fully close the gap either); it makes
the tool answer the cheaper, more attractive path, per §2 item 1: structured
fields the model does not have to parse out of prose, and a `source`
naming the exact row on every result.

Follows `app/tools/retrieve.py`'s shape closely -- clamped result count, an
explicit not-found/no-results message, a savepoint around the database
work, `ToolContext` supplying tenancy rather than any model-supplied
argument -- because both are read-only builtins answering the identical
question ("what did this tool actually find, and how do I tell the model
tenant-safely").

**The no-relevance-floor consequence, and what this module does about it
(task-5-brief.md, "The design decision I want argued, not defaulted").**
`ProductSearchService.search` (Task 4) deliberately ships with no default
relevance floor: `settings.product_search_max_cosine_distance`/
`product_search_min_keyword_rank` default to `None`, because every
offline measurement available to calibrate one comes from `HashingEmbedder`
-- a test double whose distance distribution has no principled relationship
to a production embedding model's. A calibrated default would be a
number wearing a measurement's clothes, and Task 4's own docstring
documents the measured consequence directly: an unrelated query against a
ten-row car catalogue still returns all ten rows, at cosine distance
0.9139-1.0000, while a genuine match on the same corpus scores 0.1294.

Three responses were on the table:

1. **Pick a fixed cutoff anyway** (e.g. hide any result with
   `vector_distance` above some constant). Rejected for exactly the reason
   Task 4 gave for not doing this one layer down: no number available today
   is a measurement of what a real embedder's distances mean, so any
   constant chosen now is a guess that would have to be re-guessed the
   moment a real provider is configured -- and a wrong guess fails *silently*
   in the specific way `docs/PHASE-5.md` §2 warns about: a genuinely
   relevant product just above the line vanishes with no signal to anyone
   that it happened.
2. **Do nothing and leave it to Phase 6.** Phase 6 is evaluation, and
   evaluation is exactly the machinery that could eventually calibrate a
   real threshold against a real embedder and real labelled queries -- but
   "leave it" here means a customer asking a car dealership about scuba
   gear gets three cars back, structured and confident, and the model has
   no reason not to recommend one. That is not a smaller version of the
   problem this task exists to reduce; it is the problem, reachable on the
   very first query Phase 5 answers.
3. **Surface the raw signal honestly instead of filtering on it** -- the
   option this module implements. `ProductMatch.vector_distance`/
   `.keyword_rank` already exist on the contract *specifically* so a caller
   can make this decision (Task 4's own docstring says so); the choice made
   here is to hand them to the one party who does not need a calibrated
   number to use them usefully: the model itself, on this one call, in the
   context of this one question. `_MATCH_QUALITY_NOTE` below (added to
   `content` only when a `query` actually drove ranking -- filters-only
   browsing has no relevance question to caveat) states plainly that
   results are ranked, not floored, and that a large `vector_distance`
   alongside a missing `keyword_rank` means "the least-bad option", not "a
   good match" -- and each result's `data` payload carries the literal
   numbers, per match, for the model to weigh. This reports match quality
   instead of hiding it, without smuggling a magic number in as if it had
   been measured. It does not stop a model that ignores the note from
   recommending anyway -- no prompt rule fully holds, per §2 item 2 -- but
   that is the same partial-mitigation shape every other layer of §2
   already accepts, not a new gap this module introduces.

**Citations.** `docs/ARCHITECTURE.md` §5.4 rule 3 ("message_citations
records what was actually retrieved") is implemented for products the
identical way it already is for chunks: every product that reaches the
prompt -- from either tool -- gets a `CitationPayload` with `product_id`
set (`chunk_id`/`document_id` left `None`, the widened shape
`app/rag/retrieve.py` now carries), which `ChatService._record_citations`
persists onto `message_citations.product_id`. `get_product` emits one, not
zero, for the same reason `search_products` emits one per result: a fetched
product's data reaches the prompt exactly as surely as a searched one does,
and Phase 6's whole premise (§2 item 3) is that this must be answerable
after the fact, not merely true of the tool the brief happened to name
first.
"""

import json
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.tenancy import TenantContext
from app.db.models import Product, ProductAvailability
from app.embeddings.base import EmbeddingProvider
from app.products.service import ProductService
from app.rag.products import ProductMatch, ProductSearchService
from app.rag.retrieve import CitationPayload
from app.tools.base import AgentTool, ToolContext, ToolResult

# Same reasoning as `app/tools/retrieve.py`'s `_MAX_TOP_K`: `limit` reaches
# this tool as a value a model chose, not one a trusted caller passed, so it
# is clamped to what the tool will ever hand back rather than validated and
# rejected. Twenty, not ten (retrieval's own bound): a product listing
# ("everything under £30,000") is a legitimate browse-the-catalogue request
# in a way a document search rarely is, and a product's structured entry is
# far smaller than an assembled document passage, so a wider cap costs
# proportionally less prompt budget.
_MAX_LIMIT = 20

_NO_RESULTS_MESSAGE = (
    "No products matched this search. Do not invent a product, price, or "
    "availability; tell the user nothing in the catalogue matches."
)

_NOT_FOUND_MESSAGE = "No product found with that id."

# Added to `content` only for a ranked (query-driven) search -- a
# filters-only browse has no relevance question to caveat, since every
# surviving row satisfied an exact predicate rather than a similarity
# score. See this module's own docstring for the argument behind stating
# this instead of filtering on it.
_MATCH_QUALITY_NOTE = (
    "Results are ranked by relevance to your query but are NOT filtered by "
    "a minimum relevance threshold -- the least-bad match in the catalogue "
    "is still returned even when nothing is actually a good fit. Check each "
    "result's vector_distance (lower = closer semantic match; absent means "
    "the product has no embedding yet) and keyword_rank (absent means no "
    "exact lexical overlap with your query) before presenting a result as "
    "relevant. Do not assume the first result answers the question."
)


class SearchProductsArgs(BaseModel):
    query: str | None = Field(
        default=None,
        description="A focused search query for the product catalogue -- rewrite the "
        "user's question into a standalone description of what they want, resolving "
        "pronouns against the conversation. Omit to browse by filters alone (e.g. "
        "'everything under £30,000' has no similarity query, only a price filter).",
    )
    category: str | None = Field(default=None, description="Exact category to filter to.")
    min_price: Decimal | None = Field(
        default=None, description="Minimum price (inclusive), in the catalogue's own currency."
    )
    max_price: Decimal | None = Field(
        default=None, description="Maximum price (inclusive), in the catalogue's own currency."
    )
    attributes: dict[str, Any] | None = Field(
        default=None,
        description='Exact attribute filters, e.g. {"seats": 7} -- matches a product '
        "whose own attributes are a superset of this.",
    )
    limit: int = Field(default=10, description="Maximum number of products to return.")


class GetProductArgs(BaseModel):
    product_id: uuid.UUID = Field(description="The product's id, from a prior search result.")


def _format_price(price: Decimal | None, currency: str | None) -> str:
    if price is None:
        return "price not set"
    return f"{price} {currency}" if currency else str(price)


def _match_signal_line(match: ProductMatch) -> str:
    vector = f"{match.vector_distance:.4f}" if match.vector_distance is not None else "n/a"
    keyword = f"{match.keyword_rank:.4f}" if match.keyword_rank is not None else "n/a"
    return f"   Match signal: vector_distance={vector}, keyword_rank={keyword}"


def _render_common(
    *,
    name: str,
    price: Decimal | None,
    currency: str | None,
    availability: ProductAvailability,
    stock_quantity: int | None,
    category: str | None,
    attributes: dict[str, Any],
    description: str | None,
) -> list[str]:
    lines = [f"   Price: {_format_price(price, currency)}"]
    availability_line = f"   Availability: {availability.value}"
    if stock_quantity is not None:
        availability_line += f" ({stock_quantity} in stock)"
    lines.append(availability_line)
    if category:
        lines.append(f"   Category: {category}")
    if attributes:
        lines.append(f"   Attributes: {json.dumps(attributes, sort_keys=True)}")
    if description:
        lines.append(f"   Description: {description}")
    return lines


def _render_match(rank: int, match: ProductMatch, *, ranked: bool) -> str:
    lines = [f"{rank}. {match.name} (source: product:{match.product_id})"]
    lines.extend(
        _render_common(
            name=match.name,
            price=match.price,
            currency=match.currency,
            availability=match.availability,
            stock_quantity=match.stock_quantity,
            category=match.category,
            attributes=match.attributes,
            description=match.description,
        )
    )
    if ranked:
        lines.append(_match_signal_line(match))
    return "\n".join(lines)


def _match_data(match: ProductMatch) -> dict[str, Any]:
    return {
        "source": f"product:{match.product_id}",
        "product_id": str(match.product_id),
        "external_id": match.external_id,
        "name": match.name,
        "description": match.description,
        "category": match.category,
        "price": str(match.price) if match.price is not None else None,
        "currency": match.currency,
        "attributes": match.attributes,
        "availability": match.availability.value,
        "stock_quantity": match.stock_quantity,
        "image_url": match.image_url,
        "product_url": match.product_url,
        "vector_distance": match.vector_distance,
        "keyword_rank": match.keyword_rank,
    }


def _match_citation(match: ProductMatch, rank: int) -> CitationPayload:
    return CitationPayload(
        chunk_id=None,
        document_id=None,
        product_id=match.product_id,
        document_title=match.name,
        rank=rank,
        score=match.score,
        excerpt=f"{_format_price(match.price, match.currency)} · {match.availability.value}",
        page=None,
    )


def _product_data(product: Product) -> dict[str, Any]:
    return {
        "source": f"product:{product.id}",
        "product_id": str(product.id),
        "external_id": product.external_id,
        "name": product.name,
        "description": product.description,
        "category": product.category,
        "price": str(product.price) if product.price is not None else None,
        "currency": product.currency,
        "attributes": product.attributes,
        "availability": product.availability.value,
        "stock_quantity": product.stock_quantity,
        "image_url": product.image_url,
        "product_url": product.product_url,
    }


def _product_citation(product: Product) -> CitationPayload:
    # `score`/`rank` are the query-less-search convention from
    # `app/rag/products.py::_search_filtered` (`0.0`, rank `1`): a direct
    # fetch by id has no ranking signal to report any more than a
    # filters-only browse does.
    return CitationPayload(
        chunk_id=None,
        document_id=None,
        product_id=product.id,
        document_title=product.name,
        rank=1,
        score=0.0,
        excerpt=f"{_format_price(product.price, product.currency)} · {product.availability.value}",
        page=None,
    )


def _render_product(product: Product) -> str:
    lines = [f"{product.name} (source: product:{product.id})"]
    lines.extend(
        _render_common(
            name=product.name,
            price=product.price,
            currency=product.currency,
            availability=product.availability,
            stock_quantity=product.stock_quantity,
            category=product.category,
            attributes=product.attributes,
            description=product.description,
        )
    )
    return "\n".join(lines)


class SearchProductsTool(AgentTool):
    name = "search_products"
    description = (
        "Search the organization's product catalogue by keyword, semantic similarity, "
        "and/or exact filters (category, price range, attributes). Use this -- never a "
        "document chunk -- to answer what the organization sells and what it costs. "
        "Returns structured product data (price, currency, availability, attributes), "
        "each result naming the exact product row it came from, or an explicit message "
        "when nothing matches the filters. Results are ranked, not filtered, by "
        "relevance: check each result's match-quality signal before treating it as "
        "clearly relevant."
    )
    args_model = SearchProductsArgs

    def __init__(self, session: AsyncSession, embedder: EmbeddingProvider | None = None) -> None:
        # Same idiom as `RetrieveKnowledgeTool`: the caller's own,
        # already tenant-bound session, not one this tool opens for itself.
        self.session = session
        self.embedder = embedder

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SearchProductsArgs)
        limit = max(1, min(args.limit, _MAX_LIMIT))
        tenant = TenantContext(
            organization_id=ctx.organization_id,
            user_id=None,
            role=None,
            request_id=ctx.request_id,
        )

        # See `RetrieveKnowledgeTool.execute`'s docstring for why this
        # savepoint exists verbatim: `self.session` is shared across every
        # tool call this turn makes, and a DB-level failure here must not
        # poison the rest of the turn's transaction.
        async with self.session.begin_nested():
            matches = await ProductSearchService(self.session, tenant, self.embedder).search(
                args.query,
                category=args.category,
                min_price=args.min_price,
                max_price=args.max_price,
                attributes=args.attributes,
                limit=limit,
            )

        if not matches:
            return ToolResult(content=_NO_RESULTS_MESSAGE)

        ranked = args.query is not None
        blocks = [
            _render_match(rank, match, ranked=ranked) for rank, match in enumerate(matches, start=1)
        ]
        content = "\n\n".join(blocks)
        if ranked:
            content = f"{_MATCH_QUALITY_NOTE}\n\n{content}"

        return ToolResult(
            content=content,
            data={"products": [_match_data(match) for match in matches]},
            citations=[_match_citation(match, rank) for rank, match in enumerate(matches, start=1)],
        )


class GetProductTool(AgentTool):
    name = "get_product"
    description = (
        "Fetch the full record for one product by its id -- price, currency, "
        "availability, stock, attributes and description. Use this to confirm or "
        "expand on a product `search_products` already surfaced, never a document "
        "chunk, for any fact this tool can report."
    )
    args_model = GetProductArgs

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, GetProductArgs)
        tenant = TenantContext(
            organization_id=ctx.organization_id,
            user_id=None,
            role=None,
            request_id=ctx.request_id,
        )

        try:
            async with self.session.begin_nested():
                product = await ProductService(self.session, tenant).get(args.product_id)
        except NotFoundError:
            # `ProductService.get` raises the identical `NotFoundError`
            # whether `product_id` genuinely does not exist or belongs to
            # another organization (its own docstring: "cross-tenant
            # lookups fail the same way a nonexistent id does") -- so this
            # one message is already the same for both cases, by
            # construction, not by a check added here. Two-layer tenancy
            # (`docs/ARCHITECTURE.md` §2.3) means RLS would already hide a
            # foreign row from the underlying SELECT even if this explicit
            # `organization_id` predicate were ever removed; this is Layer 1
            # on top of it, not instead of it.
            return ToolResult(content=_NOT_FOUND_MESSAGE, is_error=True)

        return ToolResult(
            content=_render_product(product),
            data={"product": _product_data(product)},
            citations=[_product_citation(product)],
        )

"""Three-way product search: exact filters, full-text, and vector
similarity, composed as AND -- `docs/PHASE-5.md` §3 is the argument for why
a sales agent needs all three ("under £30,000" is a predicate, not a
preference; "Camry LE vs Camry SE" is a lexical distinction an embedding
smooths over; "something safe for my family" has no shared vocabulary with
the listing) and §6 is the AND rule this module exists to implement: a
filter narrows the candidate set, then ranking orders what survives. A
£45,000 car is a wrong answer to "under £30,000" however well it matches
semantically.

This follows `app/rag/retrieve.py`'s shape closely -- two SQL candidate
arms (vector, keyword), fused with the shared `app/core/rrf` helpers -- but
is not built on top of it: the schema, the filter predicates, and the
query-less path (retrieval has no equivalent -- a document search always
has a query) are different enough that sharing more than the fusion
algorithm itself would mean threading products' filters through a module
whose docstring is specifically about *chunks*. Only the fusion is
genuinely the same problem in both places (Ruling 3); everything else here
is written fresh against `products`.

**AND composition, concretely.** `category`, `min_price`, `max_price` and
`attributes` are rendered into the same `WHERE` clause on *every* SQL
statement this module issues -- the query-less filter list, the vector
arm, and both forms of the keyword arm. A filter is never applied by
discarding rows after ranking: a product outside the price range is never
fetched as a ranking candidate in the first place, so it cannot appear in
the result by any path, however well it would otherwise have scored. See
`tests/integration/test_product_search.py::
test_price_filter_excludes_the_best_semantic_match` for the test built to
fail if that stops being true.

**`embedding IS NULL` is a normal transient state, not corruption**
(Task 3 writes a row before an arq job embeds it -- `docs/PHASE-5.md` §5).
Decision, made explicit here rather than left to fall out of the SQL: the
vector arm excludes such a row (`p.embedding IS NOT NULL` -- it has nothing
for `<=>` to compare against), the keyword arm does not care and still
ranks it on `search_tsv` alone, and the query-less filter path does not
care either. A freshly imported, not-yet-embedded product is therefore
still answerable by exact filters and by full text immediately; only its
semantic recall lags until the embed job runs. See
`tests/integration/test_product_search.py`'s three `test_unembedded_*`
tests for what this means in each of the three paths.

Two-layer tenancy (`docs/ARCHITECTURE.md` §2.3) applies exactly as it does
in `retrieve.py`: an explicit `organization_id` predicate on every
statement, in addition to (never instead of) RLS on the caller's session.

**No relevance floor by default, and the raw signal that makes turning
one on possible.** `retrieve.py` filters each arm to a calibrated
threshold before fusion (`settings.retrieval_max_cosine_distance`,
`settings.retrieval_min_keyword_rank`) because an unrelated query would
otherwise still return its nearest neighbours, confidently. The same
failure mode applies here -- measured directly against a car catalogue:
an unrelated query ("scuba diving gear") leaves the keyword arm silent
(`ts_rank_cd` 0.0000 throughout) but the vector arm returns all ten rows
at cosine distance 0.9139-1.0000, while a real query on the same corpus
scores 0.1294 for the right answer, tailing to 0.9293 -- the noise band
and the weak-real band overlap, exactly as `retrieve.py`'s own docstring
reports for chunks. The difference is that `retrieve.py`'s numbers came
from measuring a real embedder against a real corpus; every number
available for products offline comes from `HashingEmbedder`, a hashed
-bag-of-words test double with no principled relationship to a production
embedding model's distances. Calibrating a default from that would be a
guess wearing a measurement's clothes, so `settings.
product_search_max_cosine_distance`/`product_search_min_keyword_rank`
default to `None` (off) rather than to a number -- see their comments in
`app/core/config.py`.

Turning a floor on later needs the per-arm signal to survive past this
module, which is why `ProductMatch` carries `vector_distance` and
`keyword_rank` alongside the fused `score` -- `top_fused`'s own docstring
is explicit that the fused score carries no relevance information (rank
position only), so without the raw numbers a caller could never decide
where to draw a line; the fields exist so that decision is deferred, not
foreclosed by this contract.
"""

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.rrf import fuse_rrf, top_fused
from app.core.tenancy import TenantContext
from app.db.models import ProductAvailability
from app.embeddings.base import EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.rag.vector_sql import vector_literal

# How many candidates each ranking arm contributes before fusion, relative
# to the caller's requested `limit` -- the same "wider pool than the final
# size" reasoning as `RetrievalService.retrieve`'s `candidates` parameter:
# fusing over more candidates than `limit` is what lets a product that is
# merely decent on both arms outrank one that is merely great on a single
# arm. Not exposed as a public parameter (unlike `retrieve.py`, which has
# no fixed caller yet) because this module's interface is fixed by the
# brief to exactly `search(query=None, *, category=, min_price=,
# max_price=, attributes=, limit=10)`; a fixed multiplier is an
# implementation detail of that one method, not a knob a caller has asked
# for.
_CANDIDATE_MULTIPLIER = 4
_MIN_CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class ProductMatch:
    """One ranked (or, with no `query`, merely filtered) product.

    Carries every field `docs/PHASE-5.md` §6 says `search_products` must be
    able to report (price, availability, attributes) plus enough identity
    (`product_id`, `external_id`) for a caller to fetch the full record via
    `get_product`. `score`/`rank` are always populated for a uniform return
    shape, but `score` is `0.0` for every row in a query-less (filters-only)
    result -- there is no ranking signal to report there, only the filter
    predicate and the deterministic tie-break order (see `_search_filtered`
    below), and pretending otherwise would invite a caller to treat filter
    order as relevance.

    `vector_distance`/`keyword_rank` are the raw, un-fused per-arm signals
    (cosine distance, `ts_rank_cd`) -- `None` when a product did not appear
    in that arm's candidates at all (an unembedded row always has
    `vector_distance is None`; a product the keyword arm never matched
    always has `keyword_rank is None`), and both `None` together in a
    query-less result. See this module's docstring for why these travel
    on the contract rather than being fused away: a relevance floor is
    deliberately not decided here, and a decision deferred to a value that
    never reaches the caller is not actually deferred.
    """

    product_id: uuid.UUID
    external_id: str
    name: str
    description: str | None
    category: str | None
    price: Decimal | None
    currency: str | None
    attributes: dict[str, Any]
    availability: ProductAvailability
    stock_quantity: int | None
    image_url: str | None
    product_url: str | None
    score: float
    rank: int
    vector_distance: float | None
    keyword_rank: float | None


@dataclass(frozen=True, slots=True)
class _CandidateInfo:
    """Everything about a product except its fused score/rank -- mirrors
    `retrieve.py`'s `_CandidateInfo`, captured once from whichever arm
    first produced this row."""

    external_id: str
    name: str
    description: str | None
    category: str | None
    price: Decimal | None
    currency: str | None
    attributes: dict[str, Any]
    availability: ProductAvailability
    stock_quantity: int | None
    image_url: str | None
    product_url: str | None


# Both arm queries (and the query-less path) select the same columns in the
# same order, via this one fragment, so a row's shape does not depend on
# which statement produced it.
_SELECT_COLUMNS = (
    "p.id AS id, p.external_id AS external_id, p.name AS name, "
    "p.description AS description, p.category AS category, p.price AS price, "
    "p.currency AS currency, p.attributes AS attributes, "
    "p.availability AS availability, p.stock_quantity AS stock_quantity, "
    "p.image_url AS image_url, p.product_url AS product_url"
)


class _Filters:
    """Builds the one `WHERE` fragment (and its bind parameters) that every
    statement in a single `search()` call shares -- the mechanism behind
    AND composition. `organization_id` and `is_active = true` are
    unconditional; `category`/`min_price`/`max_price`/`attributes` are
    appended only when supplied, so an unfiltered search still gets a
    correct (if short) `WHERE` clause rather than a dangling `AND`.
    """

    def __init__(
        self,
        organization_id: uuid.UUID,
        *,
        category: str | None,
        min_price: Decimal | None,
        max_price: Decimal | None,
        attributes: dict[str, Any] | None,
    ) -> None:
        # Layer 1 of two-layer tenancy (`docs/ARCHITECTURE.md` §2.3): this
        # predicate is bound on every statement built from `self`,
        # regardless of what RLS (layer 2) on the caller's session would
        # already have done.
        clauses = ["p.organization_id = :organization_id", "p.is_active = true"]
        params: dict[str, Any] = {"organization_id": organization_id}
        if category is not None:
            clauses.append("p.category = :category")
            params["category"] = category
        if min_price is not None:
            clauses.append("p.price >= :min_price")
            params["min_price"] = min_price
        if max_price is not None:
            clauses.append("p.price <= :max_price")
            params["max_price"] = max_price
        if attributes:
            # jsonb containment against the GIN index on `products.attributes`
            # (0010_products.py) -- `{"seats": 7}` matches a row whose
            # attributes is a superset, e.g. `{"seats": 7, "fuel": "hybrid"}`.
            # Serialized to a JSON string and cast in SQL, the same pattern
            # `app/rag/vector_sql.py`'s `vector_literal` uses for an
            # embedding, rather than relying on a bind parameter's inferred
            # type to become jsonb on its own.
            clauses.append("p.attributes @> CAST(:attributes AS jsonb)")
            params["attributes"] = json.dumps(attributes)
        self.sql = " AND ".join(clauses)
        self.params = params


# The keyword arm's strict (AND) form and its OR fallback, following the
# exact pattern `app/rag/retrieve.py` establishes and explains at length
# (see that module's docstring and its `_AND_TSQUERY`/`_OR_TSQUERY`
# comments) -- not reused code (a different table, different columns), but
# the same reasoning applied fresh:
#
# - `websearch_to_tsquery`, not `to_tsquery`, on the user's raw text: it
#   tolerates stray `&`/`:`/quotes that would otherwise raise a Postgres
#   syntax error.
# - `'english'`, matching `search_tsv`'s own generation config
#   (0010_products.py) -- querying with any other configuration returns
#   zero keyword hits while the vector arm keeps answering, the exact
#   silent half-failure `docs/PHASE-5.md`'s brief calls out.
# - The strict form ANDs every query word; when it matches nothing (a
#   natural-language query almost always contains a word the corpus
#   lacks), the OR form -- built from the strict form's own parsed and
#   `::text`-rendered tsquery, `&` replaced with `|` -- is tried instead.
# - Both forms carry a `> 0` rank floor. `retrieve.py`'s comment on
#   `_KEYWORD_ALL_TERMS_SQL` explains why `> 0` specifically: a bare
#   negation ("-cat" -> `!'cat'`) is matched by `@@` against the entire
#   corpus at `ts_rank_cd` exactly 0.0, and `> 0` is precisely the boundary
#   between "matched nothing positive" and "matched something", with no
#   collateral damage to a genuine single-word match (a SKU, a proper
#   noun), which is never scored zero.
#
#   `retrieve.py`'s OR arm additionally carries a *calibrated* floor
#   (`settings.retrieval_min_keyword_rank`, 0.15) above `> 0`, to reject a
#   single incidental shared word in a long document chunk. Products get
#   the identical *mechanism*, `settings.product_search_min_keyword_rank`,
#   but it defaults to `None` (off) rather than to a number -- see this
#   module's own docstring and the setting's comment in `app/core/config.py`
#   for why no default here is calibrated, only reachable.
_AND_TSQUERY = "websearch_to_tsquery('english', :query_text)"
_OR_TSQUERY = "replace(websearch_to_tsquery('english', :query_text)::text, '&', '|')::tsquery"


def _keyword_sql(filters_sql: str, tsquery: str, *, min_rank: float | None) -> str:
    """The keyword arm's SQL for one tsquery form (`_AND_TSQUERY` or
    `_OR_TSQUERY`).

    `ts_rank_cd(...) > 0` is written unconditionally into the SQL text
    itself, never as a value this function's caller could omit or
    override -- it is the bare-negation guard described above, and a
    review round found that a prior version instead built the floor as
    either `"> 0"` *or* `">= :min_rank"` depending on whether a caller had
    configured `min_rank`. `0.0` is a legal, entirely plausible value for
    someone reading `float | None` and wanting "no floor", and
    `ts_rank_cd(...) >= 0.0` is true for every row `@@` already matched,
    including a bare negation's exact-0.0 score -- so that one legal
    setting value silently reopened the exact hole `> 0` exists to close.

    The fix is structural, not a validator rejecting `0.0`: `min_rank`,
    when supplied, can only ever *add* a second, stricter AND clause on
    top of the unconditional `> 0` -- never replace it -- so no value of
    `min_rank` (`0.0`, negative, or omitted) can ever admit a row `> 0`
    would have rejected. A validator would have defended one bad value;
    this makes the whole class of bad values structurally unreachable.
    """
    extra_floor = (
        f"AND ts_rank_cd(p.search_tsv, {tsquery}) >= :min_rank " if min_rank is not None else ""
    )
    return (
        f"SELECT {_SELECT_COLUMNS}, ts_rank_cd(p.search_tsv, {tsquery}) AS keyword_rank "
        f"FROM products p WHERE {filters_sql} "
        f"AND p.search_tsv @@ {tsquery} AND ts_rank_cd(p.search_tsv, {tsquery}) > 0 "
        f"{extra_floor}"
        f"ORDER BY ts_rank_cd(p.search_tsv, {tsquery}) DESC LIMIT :candidates"
    )


def _vector_sql(filters_sql: str, *, distance_floor: bool) -> str:
    # `p.embedding IS NOT NULL` is the vector arm's half of the unembedded
    # -row decision (module docstring): a row with no embedding has nothing
    # for `<=>` to compare against and is excluded here, not scored as an
    # arbitrary distance. `<=>` is cosine *distance* -- smaller is more
    # similar -- so `ASC` is the correct direction; reversing it would
    # surface the least relevant products in a plausible-looking order
    # (see `tests/integration/test_product_search.py::
    # test_smaller_cosine_distance_ranks_first`, built to fail if this ever
    # flips).
    #
    # `distance_floor` adds `AND ... <= :max_distance` only when
    # `settings.product_search_max_cosine_distance` is set -- absent by
    # default (this module's docstring explains why), present when a
    # caller has actually calibrated one.
    floor_clause = (
        "AND p.embedding <=> CAST(:query_vector AS vector) <= :max_distance "
        if distance_floor
        else ""
    )
    return (
        f"SELECT {_SELECT_COLUMNS}, "
        "p.embedding <=> CAST(:query_vector AS vector) AS vector_distance "
        f"FROM products p WHERE {filters_sql} "
        "AND p.embedding IS NOT NULL "
        f"{floor_clause}"
        "ORDER BY p.embedding <=> CAST(:query_vector AS vector) ASC "
        "LIMIT :candidates"
    )


def _row_to_info(row: Any) -> _CandidateInfo:
    return _CandidateInfo(
        external_id=row.external_id,
        name=row.name,
        description=row.description,
        category=row.category,
        price=row.price,
        currency=row.currency,
        attributes=row.attributes or {},
        # Raw SQL (not the ORM), so this comes back as the enum's raw
        # string value (e.g. "in_stock"), not a `ProductAvailability`
        # instance -- coerced here so `ProductMatch.availability` carries
        # the same type a caller reading `Product.availability` through
        # `ProductService` would see.
        availability=ProductAvailability(row.availability),
        stock_quantity=row.stock_quantity,
        image_url=row.image_url,
        product_url=row.product_url,
    )


def _match(
    product_id: uuid.UUID,
    info: _CandidateInfo,
    score: float,
    rank: int,
    *,
    vector_distance: float | None = None,
    keyword_rank: float | None = None,
) -> ProductMatch:
    return ProductMatch(
        product_id=product_id,
        external_id=info.external_id,
        name=info.name,
        description=info.description,
        category=info.category,
        price=info.price,
        currency=info.currency,
        attributes=info.attributes,
        availability=info.availability,
        stock_quantity=info.stock_quantity,
        image_url=info.image_url,
        product_url=info.product_url,
        score=score,
        rank=rank,
        vector_distance=vector_distance,
        keyword_rank=keyword_rank,
    )


class ProductSearchService:
    def __init__(
        self,
        session: AsyncSession,
        tenant: TenantContext,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.session = session
        self.tenant = tenant
        self.embedder = embedder or get_embedding_provider()

    async def search(
        self,
        query: str | None = None,
        *,
        category: str | None = None,
        min_price: Decimal | None = None,
        max_price: Decimal | None = None,
        attributes: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[ProductMatch]:
        """Filters always apply; ranking mode depends on `query`.

        `query is None`: filters alone, in a deterministic order --
        "everything under £30,000" is a legitimate request with nothing to
        rank by relevance. `query` present: filters narrow the candidate
        set exactly as in the query-less path, and what survives is ranked
        by fusing the keyword and vector arms with the shared RRF helpers
        (`app/core/rrf`) -- the same fusion `app/rag/retrieve.py` uses,
        because summing rank-derived scores across independent candidate
        lists is the same problem in both places (Ruling 3).
        """
        filters = _Filters(
            self.tenant.organization_id,
            category=category,
            min_price=min_price,
            max_price=max_price,
            attributes=attributes,
        )
        if query is None:
            return await self._search_filtered(filters, limit)
        return await self._search_ranked(filters, query, limit)

    async def _search_filtered(self, filters: _Filters, limit: int) -> list[ProductMatch]:
        # Deterministic order with no ranking signal available: `name` is
        # the field a human browsing "everything under £30,000" would
        # expect a catalogue to be sorted by, and `id` breaks ties (two
        # products can share a name) so the order is stable across calls
        # rather than merely "whatever the planner happened to return".
        sql = text(
            f"SELECT {_SELECT_COLUMNS} FROM products p WHERE {filters.sql} "
            "ORDER BY p.name ASC, p.id ASC LIMIT :limit"
        )
        rows = (await self.session.execute(sql, {**filters.params, "limit": limit})).all()
        return [
            _match(row.id, _row_to_info(row), 0.0, rank) for rank, row in enumerate(rows, start=1)
        ]

    async def _search_ranked(self, filters: _Filters, query: str, limit: int) -> list[ProductMatch]:
        candidates = max(limit * _CANDIDATE_MULTIPLIER, _MIN_CANDIDATES)
        [query_vector] = await self.embedder.embed([query])

        # `None` by default (see the module docstring and
        # `app/core/config.py`'s comment) -- resolved once per call, not
        # cached on `self`, so a test (or a future admin setting) that
        # changes these between calls is honoured immediately.
        settings = get_settings()
        max_distance = settings.product_search_max_cosine_distance
        min_keyword_rank = settings.product_search_min_keyword_rank

        vector_params = {
            **filters.params,
            "query_vector": vector_literal(query_vector),
            "candidates": candidates,
        }
        if max_distance is not None:
            vector_params["max_distance"] = max_distance
        vector_rows = (
            await self.session.execute(
                text(_vector_sql(filters.sql, distance_floor=max_distance is not None)),
                vector_params,
            )
        ).all()

        keyword_params = {**filters.params, "query_text": query, "candidates": candidates}
        # The strict (AND) arm never takes `min_keyword_rank` -- reaching
        # this arm at all already means every content word matched, which
        # `retrieve.py`'s own reasoning (ported in this module's comment
        # above `_keyword_sql`) establishes as a relevance predicate that
        # a calibrated-for-the-OR-arm number would only ever wrongly
        # tighten. `min_rank=None` here still gets the unconditional `> 0`
        # guard from inside `_keyword_sql` -- that part is never optional.
        keyword_rows = (
            await self.session.execute(
                text(_keyword_sql(filters.sql, _AND_TSQUERY, min_rank=None)), keyword_params
            )
        ).all()
        if not keyword_rows:
            # Only on a miss, mirroring `retrieve.py`: an exact-ish query
            # keeps the strict form's precision, and the fallback statement
            # only runs when the strict form had nothing to give.
            or_params = dict(keyword_params)
            if min_keyword_rank is not None:
                or_params["min_rank"] = min_keyword_rank
            keyword_rows = (
                await self.session.execute(
                    text(_keyword_sql(filters.sql, _OR_TSQUERY, min_rank=min_keyword_rank)),
                    or_params,
                )
            ).all()

        info: dict[uuid.UUID, _CandidateInfo] = {}
        for rows in (vector_rows, keyword_rows):
            for row in rows:
                if row.id not in info:
                    info[row.id] = _row_to_info(row)

        # The raw per-arm signal, captured separately from `info` (which is
        # "first arm to see this row wins") because a row found by *both*
        # arms has a distance *and* a rank, and both must survive -- not
        # just whichever arm happened to be iterated first.
        vector_distance_by_id = {row.id: row.vector_distance for row in vector_rows}
        keyword_rank_by_id = {row.id: row.keyword_rank for row in keyword_rows}

        scores = fuse_rrf([[row.id for row in vector_rows], [row.id for row in keyword_rows]])
        ordered = top_fused(scores, top_k=limit)

        return [
            _match(
                product_id,
                info[product_id],
                score,
                rank,
                vector_distance=vector_distance_by_id.get(product_id),
                keyword_rank=keyword_rank_by_id.get(product_id),
            )
            for rank, (product_id, score) in enumerate(ordered, start=1)
        ]

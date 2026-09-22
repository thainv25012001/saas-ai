"""Three-way product search: exact filters, full-text, and vector similarity,
composed as AND -- `docs/PHASE-5.md` §3/§6.

Every test seeds `Product` rows through `ProductService` (never raw SQL, so
`search_tsv` and `attributes`' GIN index are populated exactly the way a real
import would populate them) and embeds with `HashingEmbedder` -- a real
hashed bag-of-words embedder, not a random-vector fake (see
`app/embeddings/hashing.py`'s own docstring for why that distinction is load
-bearing for a retrieval suite). Products are embedded from their own
`embeddable_text(name, description, attributes)` wherever a test's point is
about *relevance* -- so the similarity margins asserted are the ones the
shipped embedder genuinely produces from genuine lexical overlap, not a
synthetic vector nothing but the test itself would ever produce (the
"distractor shares no vocabulary with anything" trap `docs/PHASE-5.md`'s
brief calls out).
"""

from decimal import Decimal
from typing import Any

import pytest

from app.core.tenancy import tenant_session
from app.embeddings.hashing import HashingEmbedder
from app.products.embedding_text import embeddable_text
from app.products.schemas import ProductInput
from app.products.service import ProductService
from app.rag.products import ProductSearchService

pytestmark = pytest.mark.anyio

_embedder = HashingEmbedder()


async def _embed(text_: str) -> list[float]:
    [vector] = await _embedder.embed([text_])
    return vector


async def _embed_product(
    name: str, description: str | None = None, attributes: dict[str, Any] | None = None
) -> list[float]:
    """The embedding a real import would compute for this product -- the
    exact text `app/products/embedding.py::product_embedding_text` embeds,
    not a hand-rolled concatenation that could drift from it."""
    return await _embed(embeddable_text(name, description, attributes or {}))


def _negate(vector: list[float]) -> list[float]:
    return [-v for v in vector]


def _input(**overrides: object) -> ProductInput:
    fields: dict[str, object] = {
        "external_id": "sku-1",
        "name": "Product",
        "slug": "product",
        "price": Decimal("100.00"),
        "currency": "USD",
        **overrides,
    }
    if fields.get("embedding") is not None and "embedding_model" not in overrides:
        fields["embedding_model"] = "hashing"
    return ProductInput(**fields)


async def _create(session, tenant, **overrides: object):
    return await ProductService(session, tenant).create(_input(**overrides))


# --- AND composition: the test that proves filters narrow before ranking ---


async def test_price_filter_excludes_the_best_semantic_match(tenant_a):
    """The core claim of `docs/PHASE-5.md` §6: filters and ranking compose
    as AND. `excluded` is the best possible match on *both* the keyword and
    vector arms -- real, measured lexical and semantic overlap with the
    query via `HashingEmbedder`, not a synthetic embedding -- and is priced
    above the budget. `included` shares only one query word ("safety") and
    is a plausible but lesser competitor, priced inside the budget. A
    version that ranks-then-filters (or forgets the price predicate on one
    arm) returns `excluded` first; a version that filters before ranking on
    every arm never returns it at all.
    """
    query = "spacious family suv seven seats safety rating"

    excluded_name = "Highlander Family SUV"
    excluded_description = "A spacious seven-seat SUV with a top safety rating for families."
    included_name = "Compact City Hatchback"
    included_description = "A small affordable city car with a basic safety package."

    async with tenant_session(tenant_a) as session:
        excluded = await _create(
            session,
            tenant_a,
            external_id="suv-1",
            name=excluded_name,
            slug="highlander-family-suv",
            description=excluded_description,
            price=Decimal("45000.00"),
            embedding=await _embed_product(excluded_name, excluded_description),
        )
        included = await _create(
            session,
            tenant_a,
            external_id="hatch-1",
            name=included_name,
            slug="compact-city-hatchback",
            description=included_description,
            price=Decimal("18000.00"),
            embedding=await _embed_product(included_name, included_description),
        )

    # Positive control: with no price filter, the semantically closer
    # (and lexically closer) product wins outright -- proving `excluded`
    # really is the best match this corpus can produce, so its absence
    # below is the price filter's doing and not a corpus that never
    # favoured it in the first place.
    async with tenant_session(tenant_a) as session:
        unfiltered = await ProductSearchService(session, tenant_a).search(query=query)
    assert unfiltered[0].product_id == excluded.id

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(
            query=query, max_price=Decimal("30000.00")
        )

    assert [r.product_id for r in results] == [included.id]


async def test_category_filter_excludes_other_categories(tenant_a):
    async with tenant_session(tenant_a) as session:
        sedan = await _create(
            session, tenant_a, external_id="a", name="Sedan One", category="sedan"
        )
        await _create(session, tenant_a, external_id="b", name="Truck One", category="truck")

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(category="sedan")

    assert [r.product_id for r in results] == [sedan.id]


async def test_attribute_filter_uses_jsonb_containment(tenant_a):
    async with tenant_session(tenant_a) as session:
        seven_seater = await _create(
            session,
            tenant_a,
            external_id="a",
            name="Big Van",
            attributes={"seats": 7, "fuel": "diesel"},
        )
        await _create(session, tenant_a, external_id="b", name="Small Car", attributes={"seats": 5})

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(attributes={"seats": 7})

    assert [r.product_id for r in results] == [seven_seater.id]


async def test_query_less_search_returns_filtered_products_in_deterministic_order(tenant_a):
    """ "Give me everything under £30,000" -- a legitimate request with no
    text query at all. Filters alone must still narrow the set, and the
    result order must not depend on insertion order or be arbitrary from
    one call to the next."""
    async with tenant_session(tenant_a) as session:
        cheap_b = await _create(
            session, tenant_a, external_id="b", name="Bravo", price=Decimal("20000.00")
        )
        cheap_a = await _create(
            session, tenant_a, external_id="a", name="Alpha", price=Decimal("10000.00")
        )
        await _create(session, tenant_a, external_id="c", name="Charlie", price=Decimal("50000.00"))

    async with tenant_session(tenant_a) as session:
        first_call = await ProductSearchService(session, tenant_a).search(
            max_price=Decimal("30000.00")
        )
    async with tenant_session(tenant_a) as session:
        second_call = await ProductSearchService(session, tenant_a).search(
            max_price=Decimal("30000.00")
        )

    assert [r.product_id for r in first_call] == [cheap_a.id, cheap_b.id]
    assert [r.product_id for r in second_call] == [cheap_a.id, cheap_b.id]


async def test_min_price_filter_excludes_cheaper_products(tenant_a):
    async with tenant_session(tenant_a) as session:
        pricey = await _create(
            session, tenant_a, external_id="a", name="Alpha", price=Decimal("50000.00")
        )
        await _create(session, tenant_a, external_id="b", name="Bravo", price=Decimal("5000.00"))

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(
            min_price=Decimal("30000.00")
        )

    assert [r.product_id for r in results] == [pricey.id]


# --- The `<=>` direction pin -------------------------------------------------


async def test_smaller_cosine_distance_ranks_first(tenant_a):
    """Pins the `<=>` direction. `<=>` is cosine *distance*; ascending order
    (closest first) is correct. Reversing it would rank `far` first, and
    this is the only assertion in the suite built to catch exactly that --
    both products are lexically identical filler with no shared vocabulary
    with the query, so RRF has nothing to fuse and the final order is pure
    vector order.
    """
    query = "aerodynamic carbon fiber road bicycle frame"
    query_vector = await _embed(query)
    near = query_vector
    far = _negate(query_vector)

    async with tenant_session(tenant_a) as session:
        near_product = await _create(
            session,
            tenant_a,
            external_id="near",
            name="Filler Item Alpha",
            description="Completely unrelated filler content about nothing important.",
            embedding=near,
        )
        await _create(
            session,
            tenant_a,
            external_id="far",
            name="Filler Item Beta",
            description="Completely unrelated filler content about nothing important too.",
            embedding=far,
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(query=query)

    assert len(results) == 2
    assert results[0].product_id == near_product.id
    assert results[0].score > results[1].score


async def test_chunk_found_by_both_arms_outranks_the_single_best_vector_match(tenant_a):
    """The strong form of the RRF-fusion pin, ported from
    `app/rag/retrieve.py`'s own test of the same name: `vector_best` has the
    single best individual placement (vector rank 1, distance 0) yet must
    still lose to `both`, which is merely vector rank 2 but *also* the sole
    keyword match -- only true under genuine RRF summation across arms
    (`1/61 + 1/62 > 1/61`), not a max-of-best-rank shortcut. This is the
    justification for reusing `app/core/rrf.py` rather than re-deriving a
    weaker fusion for products: the same failure mode applies to any ranked
    -list fusion, not just document chunks.
    """
    query = "extended roadside assistance coverage plan"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        vector_best = await _create(
            session,
            tenant_a,
            external_id="vb",
            name="Quarterly Sales Meeting Notes",
            description="The quarterly sales meeting starts promptly every Monday morning.",
            embedding=query_vector,
        )
        both = await _create(
            session,
            tenant_a,
            external_id="both",
            name="Roadside Assistance Plan",
            description="Extended roadside assistance coverage plan for eligible vehicles.",
            embedding=_negate(query_vector),
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(query=query)

    assert results[0].product_id == both.id
    assert results[0].score > results[1].score
    assert {r.product_id for r in results} == {vector_best.id, both.id}


# --- Raw per-arm signals and the configurable relevance floor --------------
#
# `ProductMatch.vector_distance`/`keyword_rank` are the raw, un-fused
# per-arm numbers -- added because the fused `score` alone forecloses ever
# implementing a relevance floor: `top_fused`'s own docstring says a fused
# RRF score carries no relevance information, only rank position, so
# without these fields nothing a caller does with `settings.
# product_search_max_cosine_distance`/`product_search_min_keyword_rank`
# could ever be observed. These four tests pin the raw fields; the two
# after them pin the floor settings as a *mechanism* (off by default,
# effective once configured) rather than as calibrated numbers -- see
# `app/rag/products.py`'s module docstring for why no default here is
# measured against anything but `HashingEmbedder`, and is therefore not
# calibrated at all.


async def test_ranked_match_exposes_raw_vector_distance_and_keyword_rank(tenant_a):
    query = "extended roadside assistance coverage plan"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        await _create(
            session,
            tenant_a,
            external_id="both",
            name="Roadside Assistance Plan",
            description="Extended roadside assistance coverage plan for eligible vehicles.",
            embedding=query_vector,
        )

    async with tenant_session(tenant_a) as session:
        [result] = await ProductSearchService(session, tenant_a).search(query=query)

    assert result.vector_distance == pytest.approx(0.0, abs=1e-6)
    assert result.keyword_rank is not None
    assert result.keyword_rank > 0.0


async def test_vector_only_match_has_no_keyword_rank(tenant_a):
    query = "aerodynamic carbon fiber road bicycle frame"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        await _create(
            session,
            tenant_a,
            external_id="a",
            name="Filler Item Alpha",
            description="Completely unrelated filler content about nothing important.",
            embedding=query_vector,
        )

    async with tenant_session(tenant_a) as session:
        [result] = await ProductSearchService(session, tenant_a).search(query=query)

    assert result.vector_distance == pytest.approx(0.0, abs=1e-6)
    assert result.keyword_rank is None


async def test_keyword_only_match_has_no_vector_distance(tenant_a):
    async with tenant_session(tenant_a) as session:
        await _create(
            session,
            tenant_a,
            external_id="new-import",
            name="Wireless Ergonomic Mouse",
            description="A Bluetooth mouse with adjustable DPI settings.",
            embedding=None,
        )

    async with tenant_session(tenant_a) as session:
        [result] = await ProductSearchService(session, tenant_a).search(
            query="wireless ergonomic mouse"
        )

    assert result.vector_distance is None
    assert result.keyword_rank is not None


async def test_query_less_match_has_no_raw_signals(tenant_a):
    async with tenant_session(tenant_a) as session:
        await _create(session, tenant_a, external_id="a", name="Alpha")

    async with tenant_session(tenant_a) as session:
        [result] = await ProductSearchService(session, tenant_a).search()

    assert result.vector_distance is None
    assert result.keyword_rank is None


async def test_max_cosine_distance_floor_is_off_by_default_and_configurable(tenant_a, monkeypatch):
    """The floor's mechanism, not a number: with
    `product_search_max_cosine_distance` at its default (`None`, off), a
    maximally-far, lexically unrelated product still surfaces (nothing
    else in its corpus for RRF to rank it against); configuring any floor
    excludes the identical product.
    """
    import app.rag.products as products_module
    from app.core.config import get_settings

    query = "aerodynamic carbon fiber road bicycle frame"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        far = await _create(
            session,
            tenant_a,
            external_id="far",
            name="Filler Item Beta",
            description="Completely unrelated filler content about nothing important too.",
            embedding=_negate(query_vector),
        )

    async with tenant_session(tenant_a) as session:
        off_by_default = await ProductSearchService(session, tenant_a).search(query=query)
    assert [r.product_id for r in off_by_default] == [far.id]

    # `_negate` puts `far` at cosine distance exactly 2.0 from the query --
    # any floor below that excludes it.
    configured = get_settings().model_copy(update={"product_search_max_cosine_distance": 1.0})
    monkeypatch.setattr(products_module, "get_settings", lambda: configured)

    async with tenant_session(tenant_a) as session:
        with_floor = await ProductSearchService(session, tenant_a).search(query=query)
    assert with_floor == []


async def test_min_keyword_rank_floor_is_off_by_default_and_configurable(tenant_a, monkeypatch):
    """Same mechanism, for the keyword arm's OR fallback: a weak,
    single-incidental-word match (the query's five words, only "chair"
    shared) surfaces by default and is excluded once any positive floor is
    configured -- unembedded so only the keyword arm is in play.
    """
    import app.rag.products as products_module
    from app.core.config import get_settings

    async with tenant_session(tenant_a) as session:
        weak = await _create(
            session,
            tenant_a,
            external_id="weak",
            name="Dining Room Chair",
            description="A simple wooden chair for the dining room.",
            embedding=None,
        )

    query = "premium leather office chair ergonomic lumbar support"

    async with tenant_session(tenant_a) as session:
        off_by_default = await ProductSearchService(session, tenant_a).search(query=query)
    assert [r.product_id for r in off_by_default] == [weak.id]

    configured = get_settings().model_copy(update={"product_search_min_keyword_rank": 0.99})
    monkeypatch.setattr(products_module, "get_settings", lambda: configured)

    async with tenant_session(tenant_a) as session:
        with_floor = await ProductSearchService(session, tenant_a).search(query=query)
    assert with_floor == []


# --- is_active and tenancy ---------------------------------------------------


async def test_inactive_product_never_returns(tenant_a):
    query = "premium noise cancelling headphones"
    query_vector = await _embed(query)

    async with tenant_session(tenant_a) as session:
        await _create(
            session,
            tenant_a,
            external_id="inactive",
            name="Premium Noise Cancelling Headphones",
            description="Premium noise cancelling headphones with a 30-hour battery.",
            embedding=query_vector,
            is_active=False,
        )

    async with tenant_session(tenant_a) as session:
        with_query = await ProductSearchService(session, tenant_a).search(query=query)
        without_query = await ProductSearchService(session, tenant_a).search()

    assert with_query == []
    assert without_query == []


async def test_cross_tenant_product_never_surfaces_even_as_the_best_match(tenant_a, tenant_b):
    """Org B's product is seeded as the strongest possible match -- exact
    keyword text and an identical embedding -- for org A's query. If search
    only filtered results after the fact (or not at all) instead of scoping
    the underlying queries to the tenant session's RLS, this product would
    win outright.
    """
    query = "extended service agreement coverage terms"
    query_vector = await _embed(query)

    async with tenant_session(tenant_b) as session:
        other = await _create(
            session,
            tenant_b,
            external_id="org-b",
            name="Extended Service Agreement",
            description="Extended service agreement coverage terms for all models.",
            embedding=query_vector,
        )

    async with tenant_session(tenant_a) as session:
        own = await _create(
            session,
            tenant_a,
            external_id="org-a",
            name="Basic Warranty",
            description="A basic limited warranty is included with every purchase.",
            embedding=_negate(query_vector),
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(query=query)

    assert len(results) == 1
    assert results[0].product_id == own.id
    assert other.id not in {r.product_id for r in results}


async def test_organization_id_predicate_holds_even_when_rls_is_bypassed(tenant_a, tenant_b):
    """Two-layer tenancy (`docs/ARCHITECTURE.md` §2.3): RLS (layer 2) is not
    the only thing standing between this query and another org's rows. This
    test removes it -- `app_owner`, no `app.current_org_id` set -- to isolate
    layer 1, the explicit `organization_id` predicate every arm must carry.
    Covers both code paths: the query-driven (vector+keyword) search and the
    filter-only search, since they are genuinely different statements.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    query = "extended vehicle protection plan details"
    query_vector = await _embed(query)

    async with tenant_session(tenant_b) as session:
        await _create(
            session,
            tenant_b,
            external_id="org-b",
            name="Extended Vehicle Protection Plan",
            description="Extended vehicle protection plan details for all models.",
            embedding=query_vector,
        )

    async with tenant_session(tenant_a) as session:
        own = await _create(
            session,
            tenant_a,
            external_id="org-a",
            name="Extended Vehicle Protection Plan",
            description="Extended vehicle protection plan details for all models.",
            embedding=query_vector,
        )

    unscoped_engine = create_async_engine(get_settings().migration_database_url)
    try:
        unscoped_session_factory = async_sessionmaker(unscoped_engine, expire_on_commit=False)
        async with unscoped_session_factory() as unscoped_session:
            service = ProductSearchService(unscoped_session, tenant_a)
            with_query = await service.search(query=query)
            without_query = await service.search()
    finally:
        await unscoped_engine.dispose()

    assert [r.product_id for r in with_query] == [own.id]
    assert [r.product_id for r in without_query] == [own.id]


# --- Unembedded rows: a normal transient state, not corruption --------------


async def test_unembedded_product_still_surfaces_via_the_keyword_arm(tenant_a):
    """Task 3's import writes rows before embedding them, so `embedding IS
    NULL` is a normal, expected transient state. Decision: the vector arm
    drops such a row (it cannot score what has no vector), the keyword arm
    ranks it on lexical match alone. A customer who imports a catalogue and
    searches immediately must get a defined answer, not an exception or a
    silently missing product.
    """
    async with tenant_session(tenant_a) as session:
        unembedded = await _create(
            session,
            tenant_a,
            external_id="new-import",
            name="Wireless Ergonomic Mouse",
            description="A Bluetooth mouse with adjustable DPI settings.",
            embedding=None,
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(
            query="wireless ergonomic mouse"
        )

    assert results, "an unembedded product must still be found via the keyword arm"
    assert results[0].product_id == unembedded.id


async def test_unembedded_product_appears_in_query_less_filtering(tenant_a):
    """Filters do not care whether a product has been embedded yet -- only
    the vector *ranking* arm does."""
    async with tenant_session(tenant_a) as session:
        unembedded = await _create(
            session, tenant_a, external_id="new-import", name="Zzz Product", embedding=None
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search()

    assert [r.product_id for r in results] == [unembedded.id]


async def test_unembedded_product_absent_from_a_purely_semantic_query(tenant_a):
    """The other side of the same decision, stated as a test rather than
    left implicit: a product with no embedding and no lexical overlap with
    the query is not found at all -- dropped, not errored, and not
    incorrectly ranked. This is the "reduced recall until embedded" cost of
    the decision above, made visible rather than accidental.
    """
    async with tenant_session(tenant_a) as session:
        await _create(
            session,
            tenant_a,
            external_id="new-import",
            name="Something Entirely Different",
            description="Nothing about this shares any words with the query below.",
            embedding=None,
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(
            query="premium leather office chair ergonomic lumbar support"
        )

    assert results == []


async def test_limit_is_respected(tenant_a):
    async with tenant_session(tenant_a) as session:
        for i in range(5):
            await _create(session, tenant_a, external_id=f"sku-{i}", name=f"Product {i}")

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(limit=2)

    assert len(results) == 2


async def test_empty_corpus_returns_empty_list_rather_than_raising(tenant_a):
    async with tenant_session(tenant_a) as session:
        assert await ProductSearchService(session, tenant_a).search(query="anything") == []
        assert await ProductSearchService(session, tenant_a).search() == []


async def test_bare_leading_hyphen_query_does_not_match_the_whole_catalogue(tenant_a):
    """`websearch_to_tsquery` reads a leading hyphen as negation, so a query
    of just `-cat` parses to `!'cat'`, which `@@` matches every product that
    merely lacks the word "cat" -- the entire catalogue below, none of which
    mentions it. Without a rank floor on the keyword arm's strict form,
    `ts_rank_cd` scores every one of those matches exactly 0.0 but nothing
    checks it, so all three products would come back as though `-cat` named
    a positive term -- ported from `app/rag/retrieve.py`'s identical guard,
    written fresh against `products` per this module's own reasoning (see
    `_keyword_sql`'s docstring).

    Found by mutation-verification, not written up front: deleting the
    `> 0` floor from `_KEYWORD_SQL_TEMPLATE` passed the rest of this suite
    unchanged, which is exactly the gap this test exists to close.

    Products are seeded with no embedding, which isolates the keyword arm
    cleanly -- an unembedded row is dropped from the vector arm entirely
    (see the `test_unembedded_*` tests above), so anything surfacing here
    can only have come from the keyword arm's own behaviour.
    """
    async with tenant_session(tenant_a) as session:
        await _create(
            session, tenant_a, external_id="a", name="Quarterly Sales Report", embedding=None
        )
        await _create(session, tenant_a, external_id="b", name="Remote Work Policy", embedding=None)
        await _create(
            session, tenant_a, external_id="c", name="Parking Garage Hours", embedding=None
        )

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(query="-cat")

    assert results == []


@pytest.mark.parametrize("min_keyword_rank", [0.0, -1.0, None])
async def test_bare_negation_excluded_regardless_of_min_keyword_rank_value(
    tenant_a, monkeypatch, min_keyword_rank
):
    """Fix-round regression: a prior version built the OR arm's floor as
    *either* the unconditional `"> 0"` guard *or* the configurable
    `">= :min_rank"`, never both. `product_search_min_keyword_rank = 0.0`
    is a legal, entirely plausible "no floor" value for someone reading
    `float | None` -- and `ts_rank_cd(...) >= 0.0` is true for every row
    `@@` already matched, including a bare negation's exact-0.0 score, so
    that one legal value silently reopened the hole the test above pins.
    A negative value has the identical effect for the same reason.

    `_keyword_sql` now writes `> 0` unconditionally into the SQL and
    treats `min_rank` as a second, additive `AND` clause on top of it, so
    every value below -- including `None`, the default, checked here for
    completeness -- must behave identically: still zero results.
    """
    import app.rag.products as products_module
    from app.core.config import get_settings

    async with tenant_session(tenant_a) as session:
        await _create(
            session, tenant_a, external_id="a", name="Quarterly Sales Report", embedding=None
        )
        await _create(session, tenant_a, external_id="b", name="Remote Work Policy", embedding=None)
        await _create(
            session, tenant_a, external_id="c", name="Parking Garage Hours", embedding=None
        )

    configured = get_settings().model_copy(
        update={"product_search_min_keyword_rank": min_keyword_rank}
    )
    monkeypatch.setattr(products_module, "get_settings", lambda: configured)

    async with tenant_session(tenant_a) as session:
        results = await ProductSearchService(session, tenant_a).search(query="-cat")

    assert results == []


async def test_keyword_arm_uses_the_english_search_configuration(tenant_a):
    """`search_tsv` is generated with `'english'` (0010_products.py);
    querying it with any other configuration still runs without error and
    still returns zero keyword hits while the vector arm keeps answering --
    the exact silent half-failure this task's brief names explicitly, and
    Phase 3 already hit once. 'running' stems to 'run' under `'english'`
    but not under `'simple'` or an unstemmed default -- the same word choice
    as `tests/integration/test_product_service.py::
    test_search_tsv_uses_english_configuration`, which already established
    that Postgres's English stemmer reduces "running" to "run".

    Found by mutation-verification: swapping `'english'` for `'simple'` in
    `app/rag/products.py`'s `_AND_TSQUERY`/`_OR_TSQUERY` passed the rest of
    this suite unchanged (every other keyword test here matches on an
    unstemmed word) until this test was added to close the gap.

    Isolates the keyword arm with an unembedded product, same technique as
    the bare-negation test above.
    """
    async with tenant_session(tenant_a) as session:
        product = await _create(
            session,
            tenant_a,
            external_id="shoes",
            name="Trail Shoe",
            description="Built for daily runs before work.",
            embedding=None,
        )

    async with tenant_session(tenant_a) as session:
        # `search_tsv` stores "runs" as the stemmed lexeme 'run' (measured
        # directly: `to_tsvector('english', 'daily runs')` -> 'run':2).
        # Querying with the surface form 'running' only reaches that same
        # lexeme if the query side is *also* stemmed under `'english'`
        # (measured: `websearch_to_tsquery('english', 'running')` ->
        # 'run'). Under `'simple'`, the query lexeme stays the literal
        # 'running', which the stored 'run' lexeme never matches -- this is
        # what makes the config mismatch observable rather than
        # accidentally still matching regardless of which side stems.
        results = await ProductSearchService(session, tenant_a).search(query="running")

    assert [r.product_id for r in results] == [product.id]

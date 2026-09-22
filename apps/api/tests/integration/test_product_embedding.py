"""`app/products/embedding.py`: turning product rows into vectors.

Most of these tests need no database at all -- `embed_texts`/`embed_products`
are pure functions of their input plus whichever provider `get_embedding_provider`
resolves to (the default, network-free `HashingEmbedder`, for every test
here), and `needs_reembedding` is a pure function of a `Product`'s two
embedding-state columns. They live in `tests/integration/` anyway, alongside
`test_product_service.py`, because the ones that DO need the database --
`embed_and_store`'s persistence and failure-atomicity, and
`needs_reembedding_clause`'s SQL form -- belong with them rather than split
across two files for what is one module.
"""

import pytest
from sqlalchemy import select, text

from app.core.tenancy import tenant_session
from app.db.models import Product
from app.products import embedding as embedding_module
from app.products.embedding import (
    embed_and_store,
    embed_products,
    embed_texts,
    needs_reembedding,
    needs_reembedding_clause,
    product_embedding_text,
)
from app.products.embedding_text import embeddable_text, hash_embeddable_text
from app.products.schemas import ProductInput
from app.products.service import ProductService

pytestmark = pytest.mark.anyio


def _input(**overrides: object) -> ProductInput:
    fields: dict[str, object] = {
        "external_id": "sku-1",
        "name": "Camry Hybrid LE",
        "slug": "camry-hybrid-le",
        "description": "A fuel-efficient family sedan.",
        "category": "sedan",
        "price": "32999.00",
        "currency": "USD",
        "attributes": {"seats": 5, "fuel": "hybrid"},
        **overrides,
    }
    # See the identical comment in test_product_service.py::_input --
    # `embedding_model` must travel with `embedding`; filled in here so
    # tests that only care about the vector itself don't each have to name
    # a provider explicitly.
    if fields.get("embedding") is not None and "embedding_model" not in overrides:
        fields["embedding_model"] = "hashing"
    return ProductInput(**fields)


def _vector(seed: float) -> list[float]:
    return [seed] * 1536


def _cosine(a: list[float], b: list[float]) -> float:
    # Both `HashingEmbedder` outputs are already L2-normalized, so the dot
    # product IS the cosine similarity -- no norms to divide by here.
    return sum(x * y for x, y in zip(a, b, strict=True))


# ---------------------------------------------------------------------------
# embeddable text: what travels into the vector, what deliberately doesn't
# ---------------------------------------------------------------------------


async def test_product_embedding_text_calls_task_1s_embeddable_text():
    """Not a style check: if this module ever built its own concatenation
    instead of calling `embeddable_text`, `embedding_source_hash` (a hash of
    `embeddable_text`'s output) would certify a vector computed from
    different text than the one actually stored -- silently, forever."""
    product = Product(name="Camry", description="A sedan.", attributes={"seats": 5})
    assert product_embedding_text(product) == embeddable_text(
        product.name, product.description, product.attributes
    )


async def test_product_embedding_text_is_unaffected_by_price():
    """docs/PHASE-5.md §4: price is deliberately excluded so a price change
    stays a plain UPDATE rather than forcing a re-embed. Built from two
    Product rows differing only in `price`, not two calls to
    `embeddable_text` with identical arguments -- the latter would prove
    nothing about whether price actually reaches the text."""
    cheap = Product(name="Camry", description="A sedan.", attributes={"seats": 5}, price=1)
    expensive = Product(name="Camry", description="A sedan.", attributes={"seats": 5}, price=999999)
    assert product_embedding_text(cheap) == product_embedding_text(expensive)


async def test_product_embedding_text_changes_with_description():
    original = Product(name="Camry", description="A sedan.", attributes={})
    edited = Product(name="Camry", description="A totally different SUV.", attributes={})
    assert product_embedding_text(original) != product_embedding_text(edited)


async def test_embed_products_embeds_each_rows_embeddable_text():
    """`embed_products` is a thin wrapper: the vector for each `Product` row
    must be identical to embedding that row's own `product_embedding_text`
    directly, in the same order the rows were given."""
    camry = Product(name="Camry", description="A sedan.", attributes={"seats": 5})
    corolla = Product(name="Corolla", description="A smaller sedan.", attributes={"seats": 5})

    vectors, model = await embed_products([camry, corolla])
    expected, _ = await embed_texts(
        [product_embedding_text(camry), product_embedding_text(corolla)]
    )

    assert vectors == expected
    assert model == "hashing"


async def test_embed_texts_is_deterministic():
    """Re-embedding a product whose stable fields are unchanged must
    produce the same vector -- otherwise "does this need a re-embed"
    (`needs_reembedding`) could never settle on "no"."""
    content = "Wireless Bluetooth Headphones\nOver-ear, noise cancelling.\n{}"
    first, _ = await embed_texts([content])
    second, _ = await embed_texts([content])
    assert first == second


# ---------------------------------------------------------------------------
# Similarity: built so the wrong answer is a plausible competitor, not a
# distractor sharing no vocabulary at all (Phase 3's lesson, per the brief).
# ---------------------------------------------------------------------------


async def test_two_products_sharing_vocabulary_score_higher_than_a_plausible_competitor():
    headphones = embeddable_text(
        "Wireless Bluetooth Over-Ear Headphones",
        "Over-ear wireless headphones with active noise cancellation and a "
        "thirty hour battery life.",
        {"color": "black", "connectivity": "bluetooth"},
    )
    earbuds = embeddable_text(
        "Bluetooth Wireless In-Ear Earbuds",
        "Compact wireless earbuds with active noise cancellation and a twenty hour battery life.",
        {"color": "white", "connectivity": "bluetooth"},
    )
    # A plausible competitor for shelf space, not a random unrelated item:
    # another small black wireless accessory, sharing "wireless", "battery",
    # "life" and "color"/"black" with `headphones` -- but none of bluetooth
    # audio's specific vocabulary (bluetooth, noise, cancellation, hour).
    charger = embeddable_text(
        "Wireless Phone Charging Pad",
        "Fast wireless charging pad with a sleek design and durable long "
        "battery life for everyday use.",
        {"color": "black", "connectivity": "usb-c"},
    )

    vectors, _ = await embed_texts([headphones, earbuds, charger])
    [headphones_vec, earbuds_vec, charger_vec] = vectors

    same_category = _cosine(headphones_vec, earbuds_vec)
    competitor = _cosine(headphones_vec, charger_vec)

    assert same_category > competitor
    # Not just "higher than a random distractor" -- the competitor must
    # still register real, non-trivial similarity (it shares real
    # vocabulary), or this assertion would pass trivially against a
    # near-zero floor the way a no-shared-words distractor would.
    assert competitor > 0.1


# ---------------------------------------------------------------------------
# Batching and retry -- mirrors app/rag/ingest.py's `_embed_all` tests.
# ---------------------------------------------------------------------------


async def test_embed_texts_splits_into_batches_of_the_configured_size(monkeypatch):
    calls: list[int] = []

    class _RecordingProvider:
        name = "recording"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(len(texts))
            return [[0.0] * 1536 for _ in texts]

    tiny_batches = embedding_module.get_settings().model_copy(update={"embedding_batch_size": 2})
    monkeypatch.setattr(embedding_module, "get_settings", lambda: tiny_batches)
    monkeypatch.setattr(embedding_module, "get_embedding_provider", lambda: _RecordingProvider())

    vectors, model = await embed_texts(["a", "b", "c", "d", "e"])

    assert calls == [2, 2, 1]
    assert len(vectors) == 5
    assert model == "recording"


async def test_embed_texts_empty_input_never_calls_the_provider(monkeypatch):
    calls = {"n": 0}

    class _ShouldNeverBeCalled:
        name = "should-not-be-called"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls["n"] += 1
            return [[0.0] * 1536 for _ in texts]

    monkeypatch.setattr(embedding_module, "get_embedding_provider", lambda: _ShouldNeverBeCalled())

    vectors, model = await embed_texts([])

    assert vectors == []
    assert model == "should-not-be-called"
    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# embed_and_store: persistence, mixed-provider recording, and the
# no-partial-catalogue guarantee.
# ---------------------------------------------------------------------------


async def test_embed_and_store_persists_vector_model_and_hash(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        product = await service.create(_input(external_id="sku-1", embedding=None))
        assert product.embedding is None

        await embed_and_store(session, [product])

    assert product.embedding is not None
    assert len(product.embedding) == 1536
    assert product.embedding_model == "hashing"
    assert product.embedding_source_hash == hash_embeddable_text(
        product.name, product.description, product.attributes
    )
    assert product.embedding_stale is False


async def test_embed_and_store_clears_a_previously_stale_flag(tenant_a):
    """The other side of `needs_reembedding`: a row flagged stale by
    `upsert_many` (an embedding-less write over changed content) must
    actually clear once `embed_and_store` gives it a fresh vector computed
    from its current text -- otherwise a reconciliation job re-embedding
    exactly the rows `needs_reembedding` pointed it at would leave them
    reporting stale forever."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        await service.upsert_many(
            [_input(external_id="sku-1", description="Old.", embedding=_vector(0.2))]
        )
        [stale] = await service.upsert_many(
            [_input(external_id="sku-1", description="New.", embedding=None)]
        )
        assert stale.embedding_stale is True

        await embed_and_store(session, [stale])

    assert stale.embedding_stale is False
    assert stale.embedding_source_hash == hash_embeddable_text(
        stale.name, stale.description, stale.attributes
    )


async def test_embed_and_store_records_whichever_providers_name_is_active(tenant_a, monkeypatch):
    """A mixed-provider catalogue (a provider switch mid-import, or two
    products embedded under different configurations) is only detectable if
    the recorded name genuinely reflects what computed the vector, not a
    hardcoded string this module happens to use in the common case."""

    class _NamedProvider:
        name = "custom-provider-v2"
        dimensions = 1536

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 1536 for _ in texts]

    monkeypatch.setattr(embedding_module, "get_embedding_provider", lambda: _NamedProvider())

    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        product = await service.create(_input(external_id="sku-1", embedding=None))
        await embed_and_store(session, [product])

    assert product.embedding_model == "custom-provider-v2"


async def test_embed_and_store_failed_batch_leaves_no_partial_writes(
    tenant_a, owner_connection, monkeypatch
):
    """Mirrors `tests/integration/test_ingest.py`'s embedding-failure test:
    force `embedding_batch_size` to 1 so two products become two separate
    embedding calls, let the first succeed and the second exhaust its
    retries, and prove that leaves BOTH products unembedded -- not the one
    whose batch happened to succeed first."""
    calls = {"n": 0}

    class _FailsAfterFirstBatch:
        name = "flaky-for-test"

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls["n"] += 1
            if calls["n"] == 1:
                return [[0.0] * 1536 for _ in texts]
            raise RuntimeError("embedding backend unavailable")

    tiny_batches = embedding_module.get_settings().model_copy(
        update={"embedding_batch_size": 1, "embedding_retry_backoff_seconds": 0.0}
    )
    monkeypatch.setattr(embedding_module, "get_settings", lambda: tiny_batches)
    monkeypatch.setattr(embedding_module, "get_embedding_provider", lambda: _FailsAfterFirstBatch())

    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        first = await service.create(_input(external_id="sku-1", embedding=None))
        second = await service.create(_input(external_id="sku-2", embedding=None))

        with pytest.raises(RuntimeError):
            await embed_and_store(session, [first, second])

    # One successful batch plus `embedding_max_retries` failed attempts on
    # the second -- proves the retry loop actually retried.
    assert calls["n"] == 1 + tiny_batches.embedding_max_retries

    result = await owner_connection.execute(
        text(
            "SELECT embedding_model, embedding FROM products "
            "WHERE organization_id = :org AND external_id IN ('sku-1', 'sku-2')"
        ),
        {"org": tenant_a.organization_id},
    )
    rows = result.all()
    assert len(rows) == 2
    # Not just "still None" for the row whose batch failed -- the row from
    # the batch that DID succeed must also be untouched, since the failure
    # happened before `embed_and_store` ever started assigning attributes.
    assert all(row.embedding is None and row.embedding_model is None for row in rows)


# ---------------------------------------------------------------------------
# needs_reembedding: the three states, named.
# ---------------------------------------------------------------------------


async def test_needs_reembedding_is_true_when_never_embedded():
    """`embedding_source_hash IS NULL` -- expected mid a two-phase import,
    nothing wrong yet, but still something to do."""
    product = Product(embedding_source_hash=None, embedding_stale=False)
    assert needs_reembedding(product) is True


async def test_needs_reembedding_is_false_when_current():
    product = Product(embedding_source_hash="a" * 64, embedding_stale=False)
    assert needs_reembedding(product) is False


async def test_needs_reembedding_is_true_when_stale():
    """Embedded, but from different text -- actively wrong, not merely
    incomplete."""
    product = Product(embedding_source_hash="a" * 64, embedding_stale=True)
    assert needs_reembedding(product) is True


async def test_needs_reembedding_clause_matches_the_predicate_against_real_rows(tenant_a):
    """The SQL and Python forms must agree, exercised against genuinely
    persisted rows in all three states rather than asserted independently
    of each other."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        never_embedded = await service.create(_input(external_id="never", embedding=None))
        current = await service.create(
            _input(external_id="current", name="Fresh", embedding=[0.1] * 1536)
        )
        [stale] = await service.upsert_many(
            [_input(external_id="stale", description="Old.", embedding=[0.2] * 1536)]
        )
        [stale] = await service.upsert_many(
            [_input(external_id="stale", description="New.", embedding=None)]
        )
        assert stale.embedding_stale is True

        for product in (never_embedded, current, stale):
            assert needs_reembedding(product) is (product.external_id != "current")

        result = await session.execute(
            select(Product.external_id)
            .where(Product.organization_id == tenant_a.organization_id)
            .where(needs_reembedding_clause())
        )
        needing = {row[0] for row in result.all()}

    assert needing == {"never", "stale"}


async def test_upsert_many_round_trips_embedding_model(tenant_a):
    """Sanity check that the column actually round-trips through a real
    upsert (not just `create`), the way Task 3's import will use it."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        [product] = await service.upsert_many(
            [_input(external_id="sku-1", embedding=[0.3] * 1536, embedding_model="openai")]
        )
    assert product.embedding_model == "openai"


async def test_upsert_many_price_update_omitting_embedding_preserves_the_model(tenant_a):
    """`embedding_model` gets the identical `COALESCE` as `embedding` itself
    (same reason: it names the space the vector lives in), so a price-only
    sync that omits `embedding` must not also blank out the provider name a
    previous import recorded -- mirrors
    test_product_service.py::test_upsert_many_price_update_omitting_embedding_preserves_it
    for the new column."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        [original] = await service.upsert_many(
            [_input(external_id="sku-1", embedding=_vector(0.25), embedding_model="openai")]
        )
        assert original.embedding_model == "openai"

        [updated] = await service.upsert_many(
            [_input(external_id="sku-1", price="999.00", embedding=None)]
        )

    assert float(updated.price) == 999.00
    assert updated.embedding_model == "openai"


async def test_embedding_and_embedding_model_must_travel_together():
    with pytest.raises(ValueError):
        _input(embedding=[0.1] * 1536, embedding_model=None)
    with pytest.raises(ValueError):
        _input(embedding=None, embedding_model="hashing")

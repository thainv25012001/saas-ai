import pytest
from sqlalchemy import text

from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.db.models import ProductAvailability
from app.products.schemas import ProductInput
from app.products.service import ProductService

pytestmark = pytest.mark.anyio


def _vector(seed: float = 0.0) -> list[float]:
    return [seed] * 1536


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
    return ProductInput(**fields)


async def _product(session, tenant, **overrides):
    return await ProductService(session, tenant).create(_input(**overrides))


async def test_create_returns_a_product_scoped_to_the_tenant(tenant_a):
    async with tenant_session(tenant_a) as session:
        product = await _product(session, tenant_a)
    assert product.organization_id == tenant_a.organization_id
    assert product.availability == ProductAvailability.IN_STOCK
    assert product.is_active is True


async def test_get_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        product = await _product(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ProductService(session, tenant_b).get(product.id)


async def test_list_products_from_another_tenant_returns_empty(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await _product(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        products = await ProductService(session, tenant_b).list_products()
    assert products == []


async def test_delete_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        product = await _product(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ProductService(session, tenant_b).delete(product.id)


async def test_delete_removes_the_row(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        product = await _product(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        await ProductService(session, tenant_a).delete(product.id)

    result = await owner_connection.execute(
        text("SELECT COUNT(*) FROM products WHERE id = :id"), {"id": product.id}
    )
    assert result.scalar_one() == 0


async def test_upsert_many_creates_new_products(tenant_a):
    async with tenant_session(tenant_a) as session:
        created = await ProductService(session, tenant_a).upsert_many(
            [_input(external_id="a"), _input(external_id="b", name="Corolla")]
        )
    assert {p.external_id for p in created} == {"a", "b"}
    assert all(p.organization_id == tenant_a.organization_id for p in created)


async def test_upsert_many_same_external_id_updates_instead_of_duplicating(
    tenant_a, owner_connection
):
    """The UNIQUE (organization_id, external_id) constraint is what makes a
    second import of the same external_id an update, not a duplicate row --
    this is the mechanism Task 3's re-import upsert depends on."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        await service.upsert_many([_input(external_id="sku-1", name="Original Name")])
        await service.upsert_many([_input(external_id="sku-1", name="Renamed")])

    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*), MAX(name) FROM products "
            "WHERE organization_id = :org AND external_id = 'sku-1'"
        ),
        {"org": tenant_a.organization_id},
    )
    count, name = result.one()
    assert count == 1
    assert name == "Renamed"


async def test_upsert_many_price_update_omitting_embedding_preserves_it(tenant_a):
    """A price/stock sync (docs/PHASE-5.md §4) must be a plain UPDATE: a
    caller that has not recomputed an embedding (embedding=None) must not
    blank out the vector a previous import already computed."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        [original] = await service.upsert_many(
            [_input(external_id="sku-1", embedding=_vector(0.25))]
        )
        assert original.embedding is not None

        [updated] = await service.upsert_many(
            [_input(external_id="sku-1", price="999.00", embedding=None)]
        )

    assert float(updated.price) == 999.00
    assert updated.embedding is not None
    assert list(updated.embedding) == pytest.approx(_vector(0.25))


async def test_upsert_many_with_new_embedding_replaces_the_stale_vector(tenant_a):
    """The other side of the same coin: a real content edit (name/description)
    that supplies a freshly computed embedding must actually replace the old
    one, not be silently dropped by the same COALESCE that protects a
    price-only sync."""
    async with tenant_session(tenant_a) as session:
        service = ProductService(session, tenant_a)
        await service.upsert_many([_input(external_id="sku-1", embedding=_vector(0.1))])

        [updated] = await service.upsert_many(
            [_input(external_id="sku-1", name="New Name", embedding=_vector(0.9))]
        )

    assert list(updated.embedding) == pytest.approx(_vector(0.9))


async def test_upsert_many_is_scoped_to_the_tenant(tenant_a, tenant_b):
    """Two organizations importing catalogues that happen to share an
    external_id (a common SKU format) must not collide."""
    async with tenant_session(tenant_a) as session:
        await ProductService(session, tenant_a).upsert_many(
            [_input(external_id="shared-sku", name="Org A's Product")]
        )
    async with tenant_session(tenant_b) as session:
        await ProductService(session, tenant_b).upsert_many(
            [_input(external_id="shared-sku", name="Org B's Product")]
        )

    async with tenant_session(tenant_a) as session:
        [product_a] = await ProductService(session, tenant_a).list_products()
    async with tenant_session(tenant_b) as session:
        [product_b] = await ProductService(session, tenant_b).list_products()
    assert product_a.name == "Org A's Product"
    assert product_b.name == "Org B's Product"


async def test_search_tsv_uses_english_configuration(tenant_a, owner_connection):
    """Pins the 'english' text search configuration with an inflected word
    whose stem is NOT itself: 'running' vs. the query 'run'. Under 'simple'
    (or any non-stemming configuration) this column would still populate
    and still look non-empty, but querying the stem would silently match
    zero rows -- exactly the failure mode Task 4's
    `websearch_to_tsquery('english', ...)` is exposed to. See the module
    docstring in alembic/versions/0010_products.py.
    """
    async with tenant_session(tenant_a) as session:
        await _product(
            session,
            tenant_a,
            external_id="running-shoes",
            name="Running Shoes",
            description="Built for runners who train daily.",
        )

    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM products "
            "WHERE organization_id = :org "
            "AND search_tsv @@ websearch_to_tsquery('english', 'run')"
        ),
        {"org": tenant_a.organization_id},
    )
    assert result.scalar_one() == 1


async def test_search_tsv_excludes_price_stock_and_availability(tenant_a):
    """A price change alone must never move `search_tsv` -- if it fed the
    generated expression, docs/PHASE-5.md §4's "plain UPDATE" claim about
    price changes would be false at the schema level regardless of what the
    service layer does.

    Queried through the same tenant session that wrote the row, not
    `owner_connection` -- a separate connection cannot see writes made
    inside `tenant_session`'s still-open transaction (see
    docs/ARCHITECTURE.md §2.3 and `app/core/tenancy.py::tenant_session`)."""
    async with tenant_session(tenant_a) as session:
        product = await _product(session, tenant_a, external_id="sku-1")

        result = await session.execute(
            text("SELECT search_tsv::text FROM products WHERE id = :id"), {"id": product.id}
        )
        before = result.scalar_one()

        product.price = 1.0
        product.stock_quantity = 999999
        product.availability = ProductAvailability.OUT_OF_STOCK
        await session.flush()

        result = await session.execute(
            text("SELECT search_tsv::text FROM products WHERE id = :id"), {"id": product.id}
        )
        after = result.scalar_one()

    assert before == after
    assert "999999" not in before
    assert "out_of_stock" not in before


async def test_products_organization_id_and_external_id_is_unique(tenant_a):
    from sqlalchemy.exc import IntegrityError

    async with tenant_session(tenant_a) as session:
        await ProductService(session, tenant_a).create(_input(external_id="dup"))

    with pytest.raises(IntegrityError):
        async with tenant_session(tenant_a) as session:
            await ProductService(session, tenant_a).create(_input(external_id="dup"))


# ---------------------------------------------------------------------------
# Layer 1 in isolation
# ---------------------------------------------------------------------------


async def _unscoped_session():  # type: ignore[no-untyped-def]
    """A session that bypasses RLS entirely, for isolating Layer 1.

    Same pattern (and same reasoning) as
    `test_document_service.py::_unscoped_session`: every other cross-tenant
    test in this file runs both sides under an RLS-scoped `tenant_session`,
    where Layer 2 (RLS) already stops a cross-tenant read on its own -- that
    cannot tell "the service also filters by organization_id" apart from
    "RLS was doing all the work and the service filter is dead code". Only a
    session `app_owner` (this table's owner, so RLS does not apply to it,
    and with no `app.current_org_id` ever set) can isolate Layer 1.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_layer_1_predicates_hold_even_when_rls_is_bypassed(tenant_a, tenant_b):
    """docs/ARCHITECTURE.md §2.3's Layer 1, on `get`, `list_products` and
    `delete` -- the three reads/writes `ProductService` scopes by
    `organization_id` in addition to RLS."""
    async with tenant_session(tenant_b) as session:
        other = await _product(session, tenant_b, external_id="org-b-sku", name="Org B's Product")

    engine, session_factory = await _unscoped_session()
    try:
        async with session_factory() as unscoped:
            service = ProductService(unscoped, tenant_a)

            # 1. get: org A asking for org B's id by number must 404, not
            # hand back org B's row.
            with pytest.raises(NotFoundError):
                await service.get(other.id)

            # 2. list_products: org B's product must not appear in org A's
            # list.
            assert await service.list_products() == []

            # 3. delete: org A must not be able to delete org B's row by
            # guessing its id.
            with pytest.raises(NotFoundError):
                await service.delete(other.id)
    finally:
        await engine.dispose()

    # And the row must still exist -- the delete() call above must have
    # raised before ever reaching session.delete(), not deleted org B's
    # product and then raised something else.
    async with tenant_session(tenant_b) as session:
        still_there = await ProductService(session, tenant_b).get(other.id)
    assert still_there.id == other.id

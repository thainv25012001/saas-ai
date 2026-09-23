"""`products`, `productCategories` and `productImports` -- the GraphQL read
side of Phase 5 Task 6's dashboard page.

Task 5 built agent *tools* over products, not a GraphQL surface, so this is
the first read API the dashboard has for either table. The import itself
stays REST-only (`test_product_import.py`); rows here are seeded directly
through `ProductService`/`ProductImportService`, matching
`test_graphql_leads.py`'s reasoning: this is about what the read surface
returns, not about the writers.

The last section isolates Layer 1 (docs/ARCHITECTURE.md §2.3) for the new
service reads on an `app_owner` session, where RLS contributes nothing --
every other cross-tenant test here has both layers active and so cannot
tell the explicit `organization_id` predicate apart from dead code.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole, ProductAvailability
from app.main import create_app
from app.products.importer import ProductImportService
from app.products.schemas import ProductInput
from app.products.service import ProductService

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def _clean(clean_users) -> None:
    return None


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def _register(api_client: AsyncClient, email: str, org_name: str = "Ada Motors") -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Catalogue Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


def _tenant(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )


def _input(external_id: str, name: str, **overrides: object) -> ProductInput:
    fields: dict[str, object] = {
        "external_id": external_id,
        "name": name,
        "slug": external_id,
        "description": f"About {name}.",
        "category": "sedan",
        "price": "32999.00",
        "currency": "USD",
        "attributes": {"seats": 5, "fuel": "hybrid"},
        **overrides,
    }
    if fields.get("embedding") is not None:
        fields["embedding_model"] = "hashing"
    return ProductInput(**fields)


async def _product(
    org_id: uuid.UUID, external_id: str, name: str, **overrides: object
) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        product = await ProductService(session, tenant).create(
            _input(external_id, name, **overrides)
        )
    return product.id


PRODUCTS_QUERY = """
query Products(
  $search: String, $category: String, $availability: ProductAvailability,
  $limit: Int, $offset: Int
) {
  products(
    search: $search, category: $category, availability: $availability,
    limit: $limit, offset: $offset
  ) {
    id externalId name description category price currency
    attributes { key value }
    availability stockQuantity isActive searchIndex createdAt updatedAt
  }
}
"""


async def _products(api_client, token, **variables):
    response = await graphql(api_client, PRODUCTS_QUERY, variables, _auth(token))
    body = response.json()
    assert "errors" not in body, body
    return body["data"]["products"]


# ---------------------------------------------------------------------------
# products()
# ---------------------------------------------------------------------------


async def test_products_requires_authentication(api_client):
    response = await graphql(api_client, PRODUCTS_QUERY)

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_products_lists_the_callers_catalogue(api_client):
    token = await _register(api_client, "list-products@example.com")
    org_id = await _organization_id(api_client, token)
    product_id = await _product(
        org_id,
        "sku-1",
        "Camry Hybrid LE",
        stock_quantity=4,
        availability=ProductAvailability.PREORDER,
    )

    [row] = await _products(api_client, token)

    assert row["id"] == str(product_id)
    assert row["externalId"] == "sku-1"
    assert row["name"] == "Camry Hybrid LE"
    assert row["description"] == "About Camry Hybrid LE."
    assert row["category"] == "sedan"
    # A string, not a float -- `Numeric(12, 2)` is exact and a float would
    # not be; same reasoning as `Message.cost_usd`.
    assert row["price"] == "32999.00"
    assert row["currency"] == "USD"
    assert row["availability"] == "PREORDER"
    assert row["stockQuantity"] == 4
    assert row["isActive"] is True


async def test_products_attributes_are_key_value_strings(api_client):
    """`attributes` is heterogeneous jsonb. It crosses the wire as a list of
    string pairs so the dashboard renders one shape as text, never an
    arbitrary object it has to walk: a string value arrives as itself, and
    anything else as its JSON text."""
    token = await _register(api_client, "product-attrs@example.com")
    org_id = await _organization_id(api_client, token)
    await _product(
        org_id,
        "sku-attrs",
        "Attr Car",
        attributes={"fuel": "hybrid", "seats": 7, "awd": True, "trims": ["LE", "XLE"]},
    )

    [row] = await _products(api_client, token)

    pairs = {pair["key"]: pair["value"] for pair in row["attributes"]}
    assert pairs == {"fuel": "hybrid", "seats": "7", "awd": "true", "trims": '["LE", "XLE"]'}


async def test_products_is_scoped_to_the_callers_org(api_client):
    token_a = await _register(api_client, "products-org-a@example.com", "Ada Motors A")
    org_a = await _organization_id(api_client, token_a)
    token_b = await _register(api_client, "products-org-b@example.com", "Ada Motors B")
    org_b = await _organization_id(api_client, token_b)
    mine = await _product(org_a, "sku-a", "Org A Car")
    await _product(org_b, "sku-b", "Org B Car")

    rows = await _products(api_client, token_a)

    assert [row["id"] for row in rows] == [str(mine)]


async def test_products_search_matches_name_or_sku_case_insensitively(api_client):
    token = await _register(api_client, "product-search@example.com")
    org_id = await _organization_id(api_client, token)
    camry = await _product(org_id, "CAM-001", "Camry Hybrid LE")
    await _product(org_id, "COR-002", "Corolla Cross")

    by_name = await _products(api_client, token, search="hybrid")
    by_sku = await _products(api_client, token, search="cam-0")

    assert [row["id"] for row in by_name] == [str(camry)]
    assert [row["id"] for row in by_sku] == [str(camry)]


async def test_products_search_treats_wildcards_literally(api_client):
    """A `%` or `_` a user types is text to find, not an ILIKE wildcard --
    unescaped, `search: "%"` would match every row in the catalogue."""
    token = await _register(api_client, "product-wildcards@example.com")
    org_id = await _organization_id(api_client, token)
    discounted = await _product(org_id, "sku-pct", "Floor mats 50% off")
    await _product(org_id, "sku-plain", "Floor mats")

    rows = await _products(api_client, token, search="%")

    assert [row["id"] for row in rows] == [str(discounted)]


async def test_products_category_and_availability_filters(api_client):
    token = await _register(api_client, "product-filters@example.com")
    org_id = await _organization_id(api_client, token)
    sedan_in_stock = await _product(org_id, "sku-1", "Sedan One", category="sedan")
    await _product(
        org_id,
        "sku-2",
        "Sedan Two",
        category="sedan",
        availability=ProductAvailability.OUT_OF_STOCK,
    )
    await _product(org_id, "sku-3", "Truck One", category="truck")

    rows = await _products(api_client, token, category="sedan", availability="IN_STOCK")

    assert [row["id"] for row in rows] == [str(sedan_in_stock)]


async def test_products_limit_and_offset_page_through_the_catalogue(api_client):
    token = await _register(api_client, "product-paging@example.com")
    org_id = await _organization_id(api_client, token)
    for index in range(3):
        await _product(org_id, f"sku-{index}", f"Car {index}")

    first = await _products(api_client, token, limit=2, offset=0)
    rest = await _products(api_client, token, limit=2, offset=2)

    assert len(first) == 2
    assert len(rest) == 1
    assert {row["id"] for row in first}.isdisjoint({row["id"] for row in rest})


async def test_products_search_index_reports_all_three_embedding_states(api_client):
    """The dashboard's "not yet searchable" indicator: a product with no
    embedding yet (`embedding_source_hash IS NULL`) is invisible to the
    semantic arm of the agent's search, and one whose text changed since it
    was embedded (`embedding_stale`) is ranked on text that no longer
    describes it. Both are real states -- see `needs_reembedding`."""
    token = await _register(api_client, "product-index@example.com")
    org_id = await _organization_id(api_client, token)
    await _product(org_id, "sku-none", "Never Embedded")
    await _product(org_id, "sku-fresh", "Freshly Embedded", embedding=[0.1] * 1536)
    await _product(org_id, "sku-stale", "Soon Stale", embedding=[0.2] * 1536)
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        # An embedding-less upsert that changes the text: the stored vector
        # now describes content the row no longer has.
        await ProductService(session, tenant).upsert_many([_input("sku-stale", "Renamed")])

    rows = await _products(api_client, token)

    states = {row["externalId"]: row["searchIndex"] for row in rows}
    assert states == {"sku-none": "NOT_INDEXED", "sku-fresh": "INDEXED", "sku-stale": "STALE"}


# ---------------------------------------------------------------------------
# productCategories()
# ---------------------------------------------------------------------------

CATEGORIES_QUERY = "query { productCategories }"


async def test_product_categories_requires_authentication(api_client):
    response = await graphql(api_client, CATEGORIES_QUERY)

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_product_categories_are_distinct_sorted_and_scoped(api_client):
    token_a = await _register(api_client, "categories-a@example.com", "Ada Motors A")
    org_a = await _organization_id(api_client, token_a)
    token_b = await _register(api_client, "categories-b@example.com", "Ada Motors B")
    org_b = await _organization_id(api_client, token_b)
    await _product(org_a, "sku-1", "One", category="truck")
    await _product(org_a, "sku-2", "Two", category="sedan")
    await _product(org_a, "sku-3", "Three", category="sedan")
    await _product(org_a, "sku-4", "Four", category=None)
    await _product(org_b, "sku-5", "Other Org", category="motorbike")

    response = await graphql(api_client, CATEGORIES_QUERY, headers=_auth(token_a))

    body = response.json()
    assert "errors" not in body, body
    assert body["data"]["productCategories"] == ["sedan", "truck"]


# ---------------------------------------------------------------------------
# productImports()
# ---------------------------------------------------------------------------

IMPORTS_QUERY = """
query ProductImports($limit: Int, $offset: Int) {
  productImports(limit: $limit, offset: $offset) {
    id filename status totalRows succeededCount failedCount
    errors { row externalId message }
    error createdAt completedAt
  }
}
"""


async def _import(
    org_id: uuid.UUID,
    filename: str,
    *,
    errors: list[dict[str, object]] | None = None,
    failed_with: str | None = None,
) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        service = ProductImportService(session, tenant)
        record = await service.create(filename=filename, mime_type="text/csv")
        if failed_with is not None:
            await service.mark_failed(record.id, failed_with)
        elif errors is not None:
            await service.mark_completed(
                record.id,
                total_rows=10,
                succeeded_count=10 - len(errors),
                failed_count=len(errors),
                errors=errors,
            )
    return record.id


async def test_product_imports_requires_authentication(api_client):
    response = await graphql(api_client, IMPORTS_QUERY)

    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_product_imports_carry_counts_and_per_row_errors(api_client):
    """ "4 rows failed" is not actionable -- which rows, and why, is. The row
    numbers are Task 3's human-visible, header-aware ones, passed through
    untouched."""
    token = await _register(api_client, "imports-errors@example.com")
    org_id = await _organization_id(api_client, token)
    import_id = await _import(
        org_id,
        "catalogue.csv",
        errors=[
            {"row": 3, "external_id": "sku-3", "message": "price: not a number"},
            {"row": 7, "external_id": None, "message": "external_id: required"},
        ],
    )

    response = await graphql(api_client, IMPORTS_QUERY, headers=_auth(token))

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["productImports"]
    assert row["id"] == str(import_id)
    assert row["filename"] == "catalogue.csv"
    assert row["status"] == "COMPLETED"
    assert (row["totalRows"], row["succeededCount"], row["failedCount"]) == (10, 8, 2)
    assert row["errors"] == [
        {"row": 3, "externalId": "sku-3", "message": "price: not a number"},
        {"row": 7, "externalId": None, "message": "external_id: required"},
    ]
    assert row["completedAt"] is not None


async def test_product_import_errors_are_by_row_number_and_bounded(api_client):
    """The importer appends errors by the phase that caught them (parse,
    then duplicate, then write), so the stored list is not in file order;
    and it is unbounded, while the dashboard polls this query. `errors`
    sorts by row and takes a `limit` -- `failedCount` keeps the total."""
    token = await _register(api_client, "imports-bounded@example.com")
    org_id = await _organization_id(api_client, token)
    await _import(
        org_id,
        "many-errors.csv",
        errors=[
            {"row": 9, "external_id": "sku-9", "message": "write failed"},
            {"row": 2, "external_id": "sku-2", "message": "price: not a number"},
            {"row": 5, "external_id": "sku-5", "message": "duplicate external_id"},
        ],
    )
    query = "query { productImports { failedCount errors(limit: 2) { row } } }"

    response = await graphql(api_client, query, headers=_auth(token))

    body = response.json()
    assert "errors" not in body, body
    [row] = body["data"]["productImports"]
    assert row["failedCount"] == 3
    assert [error["row"] for error in row["errors"]] == [2, 5]


async def test_product_imports_surface_a_whole_file_failure(api_client):
    token = await _register(api_client, "imports-failed@example.com")
    org_id = await _organization_id(api_client, token)
    await _import(org_id, "broken.csv", failed_with="missing required column: name")

    response = await graphql(api_client, IMPORTS_QUERY, headers=_auth(token))

    [row] = response.json()["data"]["productImports"]
    assert row["status"] == "FAILED"
    assert row["error"] == "missing required column: name"
    assert row["totalRows"] is None
    assert row["errors"] == []


async def test_product_imports_are_newest_first_and_scoped(api_client):
    token_a = await _register(api_client, "imports-a@example.com", "Ada Motors A")
    org_a = await _organization_id(api_client, token_a)
    token_b = await _register(api_client, "imports-b@example.com", "Ada Motors B")
    org_b = await _organization_id(api_client, token_b)
    first = await _import(org_a, "first.csv")
    second = await _import(org_a, "second.csv")
    await _import(org_b, "other-org.csv")

    response = await graphql(api_client, IMPORTS_QUERY, headers=_auth(token_a))

    rows = response.json()["data"]["productImports"]
    assert [row["id"] for row in rows] == [str(second), str(first)]
    assert rows[0]["status"] == "PENDING"


# ---------------------------------------------------------------------------
# Layer 1 alone -- RLS bypassed
# ---------------------------------------------------------------------------


async def _unscoped_session():  # type: ignore[no-untyped-def]
    """An `app_owner` session: the tables' owner, so RLS does not apply and
    no `app.current_org_id` is ever set. Same pattern as
    `test_product_service.py::_unscoped_session`."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_layer_1_predicates_hold_on_the_dashboard_reads(tenant_a, tenant_b):
    """`list_products` with every filter set, `list_categories` and
    `list_imports` must each filter on `organization_id` themselves, not
    lean on RLS."""
    async with tenant_session(tenant_b) as session:
        await ProductService(session, tenant_b).create(
            _input("org-b-sku", "Org B Hybrid", category="sedan")
        )
        await ProductImportService(session, tenant_b).create(
            filename="org-b.csv", mime_type="text/csv"
        )

    engine, session_factory = await _unscoped_session()
    try:
        async with session_factory() as unscoped:
            products = ProductService(unscoped, tenant_a)
            assert (
                await products.list_products(
                    search="hybrid",
                    category="sedan",
                    availability=ProductAvailability.IN_STOCK,
                )
                == []
            )
            assert await products.list_categories() == []
            assert await ProductImportService(unscoped, tenant_a).list_imports() == []
    finally:
        await engine.dispose()

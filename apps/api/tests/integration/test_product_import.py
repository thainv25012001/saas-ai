"""`POST /api/v1/products/import`, `GET /api/v1/products/import/{id}`, and
the `import_products_task` worker job -- Task 3, end to end.

The API tests monkeypatch `products_api.enqueue_product_import` (like
`test_documents_api.py` does for `enqueue_ingest`): the production function
opens a real Redis connection pool, and no test in this suite may touch the
network. The pipeline tests below call `import_products_task` directly
instead of going through arq -- the same shape `test_title_conversation_task.py`
and `test_enqueue_ingest_live.py`'s sibling tests use -- so "an import that
actually completes end to end" is proven without a worker process or Redis
consumption: rows land in `products` with real 1536-dim vectors from
`HashingEmbedder` (no network), and the import record ends up `completed`
with the counts and per-row errors a customer would read back.
"""

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import products as products_api
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole, Product
from app.main import create_app
from app.products import embedding as embedding_module
from app.workers.tasks import import_products_task

pytestmark = pytest.mark.anyio

IMPORT_URL = "/api/v1/products/import"


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, product_import_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.calls.append((product_import_id, organization_id))


@pytest.fixture
def queue(monkeypatch) -> _RecordingQueue:
    recorder = _RecordingQueue()
    monkeypatch.setattr(products_api, "enqueue_product_import", recorder)
    return recorder


@pytest.fixture
async def api_client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _register(api_client: AsyncClient, email: str, org_name: str) -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Imports Owner",
            "organization_name": org_name,
        },
    )
    assert response.status_code == 201, response.text
    token: str = response.json()["access_token"]
    return token


async def _organization_id(api_client: AsyncClient, token: str) -> uuid.UUID:
    me = await api_client.get("/api/v1/auth/me", headers=_auth(token))
    assert me.status_code == 200, me.text
    return uuid.UUID(me.json()["organization_id"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )


async def _upload(
    api_client: AsyncClient,
    token: str,
    *,
    content: bytes,
    content_type: str = "text/csv",
    filename: str = "catalogue.csv",
):
    files = {"file": (filename, content, content_type)}
    return await api_client.post(IMPORT_URL, headers=_auth(token), files=files)


async def _run_import(org_id: uuid.UUID, product_import_id: uuid.UUID) -> None:
    """Run the worker job directly -- see the module docstring for why this,
    not arq, is what proves the pipeline actually completes."""
    await import_products_task(
        {}, organization_id=str(org_id), product_import_id=str(product_import_id)
    )


async def _products_for(org_id: uuid.UUID) -> list[Product]:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(Product).where(Product.organization_id == org_id).order_by(Product.external_id)
        )
        return list(result.scalars().all())


_VALID_CSV = (
    b"external_id,name,description,category,price,currency,attributes\n"
    b'sku-1,Camry Hybrid LE,"A fuel-efficient family sedan.",sedan,32999.00,USD,"{""seats"": 5}"\n'
    b'sku-2,Corolla LE,"A compact, reliable sedan.",sedan,24999.00,USD,"{""seats"": 5}"\n'
    b'sku-3,RAV4 XLE,"A versatile compact SUV.",suv,28999.00,USD,"{""seats"": 5}"\n'
)


# ---------------------------------------------------------------------------
# 1. Upload -> 202 + enqueue
# ---------------------------------------------------------------------------


async def test_upload_returns_202_and_a_pending_import(api_client, clean_users, queue):
    token = await _register(api_client, "imp1@example.com", "Ada Motors Import Up1")
    response = await _upload(api_client, token, content=_VALID_CSV)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["mime_type"] == "text/csv"
    assert body["succeeded_count"] == 0
    assert body["failed_count"] == 0
    assert body["errors"] == []


async def test_upload_enqueues_exactly_one_job(api_client, clean_users, queue):
    token = await _register(api_client, "imp2@example.com", "Ada Motors Import Up2")
    org_id = await _organization_id(api_client, token)
    response = await _upload(api_client, token, content=_VALID_CSV)
    import_id = uuid.UUID(response.json()["id"])

    assert queue.calls == [(import_id, org_id)]


async def test_unsupported_mime_type_is_422(api_client, clean_users, queue):
    token = await _register(api_client, "impmime@example.com", "Ada Motors Import Mime")
    response = await _upload(
        api_client,
        token,
        content=b"not a catalogue",
        content_type="application/zip",
        filename="x.zip",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unsupported_import_type"
    assert queue.calls == []


async def test_upload_without_a_file_part_is_422(api_client, clean_users, queue):
    token = await _register(api_client, "impnofile@example.com", "Ada Motors Import NoFile")
    response = await api_client.post(IMPORT_URL, headers=_auth(token))
    assert response.status_code == 422, response.text
    assert queue.calls == []


async def test_upload_without_a_token_is_401(api_client, clean_users, queue):
    files = {"file": ("catalogue.csv", _VALID_CSV, "text/csv")}
    response = await api_client.post(IMPORT_URL, files=files)
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 2. GET the import record -- pending, then completed after the worker runs
# ---------------------------------------------------------------------------


async def test_get_import_before_processing_is_pending(api_client, clean_users, queue):
    token = await _register(api_client, "imppending@example.com", "Ada Motors Import Pending")
    upload = await _upload(api_client, token, content=_VALID_CSV)
    import_id = upload.json()["id"]

    response = await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"


async def test_get_a_nonexistent_import_is_404(api_client, clean_users, queue):
    token = await _register(api_client, "impnone@example.com", "Ada Motors Import None")
    response = await api_client.get(f"{IMPORT_URL}/{uuid.uuid4()}", headers=_auth(token))
    assert response.status_code == 404


async def test_org_b_cannot_see_org_as_import(api_client, clean_users, queue):
    token_a = await _register(api_client, "impisoa@example.com", "Ada Motors Import IsoA")
    upload = await _upload(api_client, token_a, content=_VALID_CSV)
    import_id = upload.json()["id"]

    token_b = await _register(api_client, "impisob@example.com", "Ada Motors Import IsoB")
    response = await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token_b))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 3. The whole pipeline: upload -> worker -> rows with vectors
# ---------------------------------------------------------------------------


async def test_a_valid_csv_imports_every_row_with_a_real_embedding(api_client, clean_users, queue):
    token = await _register(api_client, "imppipe@example.com", "Ada Motors Import Pipe")
    org_id = await _organization_id(api_client, token)
    upload = await _upload(api_client, token, content=_VALID_CSV)
    import_id = uuid.UUID(upload.json()["id"])

    await _run_import(org_id, import_id)

    response = await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token))
    body = response.json()
    assert body["status"] == "completed"
    assert body["total_rows"] == 3
    assert body["succeeded_count"] == 3
    assert body["failed_count"] == 0
    assert body["errors"] == []

    products = await _products_for(org_id)
    assert {p.external_id for p in products} == {"sku-1", "sku-2", "sku-3"}
    for product in products:
        # "rows in the table with vectors", not just a 202 -- this is the
        # actual definition of done for this task.
        assert product.embedding is not None
        assert len(product.embedding) == 1536
        assert product.embedding_model == "hashing"
        assert product.embedding_source_hash is not None
        assert product.embedding_stale is False


async def test_a_bad_row_imports_n_minus_1_and_reports_the_failure(api_client, clean_users, queue):
    csv_with_a_bad_row = (
        b"external_id,name,price\n"
        b"sku-1,Camry,32999.00\n"
        b"sku-2,Corolla,not-a-number\n"
        b"sku-3,RAV4,28999.00\n"
    )
    token = await _register(api_client, "impbadrow@example.com", "Ada Motors Import BadRow")
    org_id = await _organization_id(api_client, token)
    upload = await _upload(api_client, token, content=csv_with_a_bad_row)
    import_id = uuid.UUID(upload.json()["id"])

    await _run_import(org_id, import_id)

    response = await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token))
    body = response.json()
    assert body["status"] == "completed"
    assert body["total_rows"] == 3
    assert body["succeeded_count"] == 2
    assert body["failed_count"] == 1
    [error] = body["errors"]
    assert error["row"] == 3  # header (1) + sku-1 (2) + sku-2, the bad row (3)
    assert error["external_id"] == "sku-2"
    assert "price" in error["message"]

    products = await _products_for(org_id)
    assert {p.external_id for p in products} == {"sku-1", "sku-3"}


async def test_a_json_import_completes_end_to_end(api_client, clean_users, queue):
    payload = json.dumps(
        [
            {"external_id": "sku-1", "name": "Camry", "price": "32999.00"},
            {"external_id": "sku-2", "name": "Corolla", "price": "24999.00"},
        ]
    ).encode()
    token = await _register(api_client, "impjson@example.com", "Ada Motors Import Json")
    org_id = await _organization_id(api_client, token)
    upload = await _upload(
        api_client,
        token,
        content=payload,
        content_type="application/json",
        filename="catalogue.json",
    )
    import_id = uuid.UUID(upload.json()["id"])

    await _run_import(org_id, import_id)

    response = await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token))
    body = response.json()
    assert body["status"] == "completed"
    assert body["succeeded_count"] == 2
    assert body["failed_count"] == 0

    products = await _products_for(org_id)
    assert {p.external_id for p in products} == {"sku-1", "sku-2"}
    assert all(p.embedding is not None for p in products)


# ---------------------------------------------------------------------------
# 4. Idempotence: re-importing the identical file changes nothing
# ---------------------------------------------------------------------------


async def test_reimporting_the_identical_file_leaves_the_count_unchanged_and_updates_nothing(
    api_client, clean_users, queue
):
    token = await _register(api_client, "impreplay@example.com", "Ada Motors Import Replay")
    org_id = await _organization_id(api_client, token)

    first_upload = await _upload(api_client, token, content=_VALID_CSV)
    await _run_import(org_id, uuid.UUID(first_upload.json()["id"]))
    first_products = {p.external_id: p for p in await _products_for(org_id)}
    assert len(first_products) == 3

    second_upload = await _upload(api_client, token, content=_VALID_CSV)
    await _run_import(org_id, uuid.UUID(second_upload.json()["id"]))
    second_products = {p.external_id: p for p in await _products_for(org_id)}

    assert len(second_products) == 3  # no duplicates -- upsert, not insert
    for external_id, product in second_products.items():
        original = first_products[external_id]
        assert product.id == original.id  # same row, not a new one
        assert product.name == original.name
        assert product.price == original.price
        assert list(product.embedding) == pytest.approx(list(original.embedding))


async def test_reimporting_unchanged_content_does_not_call_the_embedding_provider_again(
    api_client, clean_users, queue, monkeypatch
):
    """`needs_reembedding` is what makes a re-import cheap: a row whose
    embeddable content did not change must not be re-embedded at all on the
    second pass."""
    calls = {"n": 0}
    real_provider = embedding_module.get_embedding_provider()

    class _CountingProvider:
        name = real_provider.name

        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls["n"] += 1
            return await real_provider.embed(texts)

    token = await _register(api_client, "impcheap@example.com", "Ada Motors Import Cheap")
    org_id = await _organization_id(api_client, token)

    first_upload = await _upload(api_client, token, content=_VALID_CSV)
    await _run_import(org_id, uuid.UUID(first_upload.json()["id"]))
    assert calls["n"] == 0  # provider not yet swapped in -- baseline

    monkeypatch.setattr(embedding_module, "get_embedding_provider", lambda: _CountingProvider())

    second_upload = await _upload(api_client, token, content=_VALID_CSV)
    await _run_import(org_id, uuid.UUID(second_upload.json()["id"]))

    assert calls["n"] == 0


# ---------------------------------------------------------------------------
# 5. Cross-tenant: one org's import never touches another's rows
# ---------------------------------------------------------------------------


async def test_two_orgs_importing_the_same_external_id_do_not_collide(
    api_client, clean_users, queue
):
    """The write-path mirror of `test_upsert_many_is_scoped_to_the_tenant`:
    a shared SKU format across two organizations' catalogues must not let
    one import see or overwrite the other's row."""
    csv_a = b"external_id,name,price\nshared-sku,Org A Product,10.00\n"
    csv_b = b"external_id,name,price\nshared-sku,Org B Product,20.00\n"

    token_a = await _register(api_client, "impxa@example.com", "Ada Motors Import XA")
    org_a = await _organization_id(api_client, token_a)
    upload_a = await _upload(api_client, token_a, content=csv_a)
    await _run_import(org_a, uuid.UUID(upload_a.json()["id"]))

    token_b = await _register(api_client, "impxb@example.com", "Ada Motors Import XB")
    org_b = await _organization_id(api_client, token_b)
    upload_b = await _upload(api_client, token_b, content=csv_b)
    await _run_import(org_b, uuid.UUID(upload_b.json()["id"]))

    [product_a] = await _products_for(org_a)
    [product_b] = await _products_for(org_b)
    assert product_a.name == "Org A Product"
    assert product_b.name == "Org B Product"
    assert product_a.id != product_b.id


async def test_running_an_import_task_for_the_wrong_organization_finds_nothing(
    api_client, clean_users, queue
):
    """The job carries an organization id and opens its own tenant session
    with it, like `ingest_document_task`. A job enqueued (or tampered with)
    against the wrong organization must find no row rather than process
    someone else's uploaded catalogue -- proven by constructing the case so
    the read would have SUCCEEDED without this check (a real import exists
    under org A), not merely absent."""
    from app.core.errors import NotFoundError

    token_a = await _register(api_client, "impwrongorg@example.com", "Ada Motors Import WrongOrg")
    upload = await _upload(api_client, token_a, content=_VALID_CSV)
    import_id = uuid.UUID(upload.json()["id"])

    token_b = await _register(api_client, "impwrongorgb@example.com", "Ada Motors Import WrongOrgB")
    org_b = await _organization_id(api_client, token_b)

    with pytest.raises(NotFoundError):
        await _run_import(org_b, import_id)

    # Org A's own import must be untouched -- still pending, never even
    # marked processing by the misdirected job.
    response = await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token_a))
    assert response.json()["status"] == "pending"
    assert await _products_for(org_b) == []


# ---------------------------------------------------------------------------
# 6. Duplicate external_id within one file
# ---------------------------------------------------------------------------


async def test_a_duplicate_external_id_within_one_file_keeps_the_last_row_and_flags_the_first(
    api_client, clean_users, queue
):
    csv_with_a_duplicate = (
        b"external_id,name,price\nsku-1,First Name,1.00\nsku-1,Second Name,2.00\n"
    )
    token = await _register(api_client, "impdup@example.com", "Ada Motors Import Dup")
    org_id = await _organization_id(api_client, token)
    upload = await _upload(api_client, token, content=csv_with_a_duplicate)
    import_id = uuid.UUID(upload.json()["id"])

    await _run_import(org_id, import_id)

    body = (await api_client.get(f"{IMPORT_URL}/{import_id}", headers=_auth(token))).json()
    assert body["succeeded_count"] == 1
    assert body["failed_count"] == 1
    [error] = body["errors"]
    assert error["external_id"] == "sku-1"
    assert "row 3" in error["message"]

    [product] = await _products_for(org_id)
    assert product.name == "Second Name"


# ---------------------------------------------------------------------------
# 7. Two-layer tenancy on ProductImportService.get, RLS bypassed
# ---------------------------------------------------------------------------


async def test_layer_1_predicate_holds_even_when_rls_is_bypassed(api_client, clean_users, queue):
    """docs/ARCHITECTURE.md §2.3's Layer 1 on `ProductImportService.get`,
    isolated from RLS the same way test_product_service.py's equivalent
    test is: only a session with no `app.current_org_id` ever set (this
    table's owner) can prove the explicit `organization_id` predicate is
    what is doing the work, not RLS incidentally succeeding."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings
    from app.core.errors import NotFoundError
    from app.products.importer import ProductImportService

    token_a = await _register(api_client, "impunscoped@example.com", "Ada Motors Import Unscoped")
    org_a = await _organization_id(api_client, token_a)
    upload = await _upload(api_client, token_a, content=_VALID_CSV)
    import_id = uuid.UUID(upload.json()["id"])

    org_b_tenant = TenantContext(
        organization_id=uuid.uuid4(), user_id=None, role=MembershipRole.OWNER, request_id="test"
    )

    engine = create_async_engine(get_settings().migration_database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as unscoped:
            service = ProductImportService(unscoped, org_b_tenant)
            with pytest.raises(NotFoundError):
                await service.get(import_id)
    finally:
        await engine.dispose()

    # And org A can still read its own row -- the NotFoundError above came
    # from the predicate not matching org_b_tenant, not from the row being
    # gone.
    tenant_a = _tenant(org_a)
    async with tenant_session(tenant_a) as session:
        still_there = await ProductImportService(session, tenant_a).get(import_id)
    assert still_there.id == import_id

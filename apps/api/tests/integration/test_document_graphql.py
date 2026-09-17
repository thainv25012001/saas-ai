"""`documents`, `document`, and `deleteDocument` -- the GraphQL read/delete
side of Task 5. The upload itself is REST-only (see `test_documents_api.py`);
these tests seed rows through that same REST endpoint, with
`documents_api.enqueue_ingest` patched to a recorder so no test here
touches Redis.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import documents as documents_api
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
from app.documents.service import DocumentService
from app.main import create_app

pytestmark = pytest.mark.anyio


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, document_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.calls.append((document_id, organization_id))


@pytest.fixture(autouse=True)
def queue(monkeypatch) -> _RecordingQueue:
    recorder = _RecordingQueue()
    monkeypatch.setattr(documents_api, "enqueue_ingest", recorder)
    return recorder


@pytest.fixture(autouse=True)
async def _clean(clean_users) -> None:
    """Every test here registers at least one account. `clean_users` (top
    conftest.py) both purges leftover rows from prior runs and flushes the
    register endpoint's rate-limit counter -- without the flush, this
    module's ~10 tests x up to 2 registrations each would exhaust the
    5-per-hour register limit partway through the file.
    """
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


async def _register(api_client: AsyncClient, email: str, org_name: str) -> str:
    response = await api_client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Docs Owner",
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


async def _upload(api_client: AsyncClient, token: str, *, content: bytes, title: str) -> uuid.UUID:
    files = {"file": (f"{title}.txt", content, "text/plain")}
    response = await api_client.post(
        "/api/v1/documents", headers=_auth(token), files=files, data={"title": title}
    )
    assert response.status_code == 202, response.text
    return uuid.UUID(response.json()["id"])


async def _set_status(org_id: uuid.UUID, document_id: uuid.UUID, status: str) -> None:
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    async with tenant_session(tenant) as session:
        service = DocumentService(session, tenant)
        if status == "processing":
            await service.mark_processing(document_id)
        elif status == "ready":
            await service.mark_ready(document_id)
        elif status == "failed":
            await service.mark_failed(document_id, "boom")
        else:
            raise AssertionError(f"unhandled status {status!r}")


# ---------------------------------------------------------------------------
# documents(): org scoping and the status filter
# ---------------------------------------------------------------------------


async def test_documents_requires_authentication(api_client):
    response = await graphql(api_client, "{ documents { id } }")
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_documents_is_scoped_to_the_callers_org(api_client):
    token_a = await _register(api_client, "gqla@example.com", "Ada Motors GQL Docs A")
    token_b = await _register(api_client, "gqlb@example.com", "Ada Motors GQL Docs B")
    await _upload(api_client, token_a, content=b"org a content", title="Org A Doc")
    await _upload(api_client, token_b, content=b"org b content", title="Org B Doc")

    response = await graphql(api_client, "{ documents { title } }", headers=_auth(token_a))
    titles = [d["title"] for d in response.json()["data"]["documents"]]

    # The mutation this proves: a resolver that forgot the organization_id
    # predicate (or dropped RLS) would return both titles here, not just
    # the caller's own. Org B's document genuinely exists -- it is not
    # merely "never created" -- so this is a real isolation check, not a
    # vacuous one.
    assert titles == ["Org A Doc"]


async def test_documents_status_filter(api_client):
    token = await _register(api_client, "gqlfilter@example.com", "Ada Motors GQL Filter")
    org_id = await _organization_id(api_client, token)
    ready_id = await _upload(api_client, token, content=b"ready content", title="Ready Doc")
    await _set_status(org_id, ready_id, "processing")
    await _set_status(org_id, ready_id, "ready")
    await _upload(api_client, token, content=b"pending content", title="Pending Doc")

    response = await graphql(
        api_client,
        "query D($status: DocumentStatus) { documents(status: $status) { title status } }",
        {"status": "READY"},
        headers=_auth(token),
    )
    docs = response.json()["data"]["documents"]
    assert [d["title"] for d in docs] == ["Ready Doc"]
    assert docs[0]["status"] == "READY"

    unfiltered = await graphql(api_client, "{ documents { title } }", headers=_auth(token))
    titles = {d["title"] for d in unfiltered.json()["data"]["documents"]}
    # Proves the filter in the previous query is actually filtering, not
    # coincidentally returning one row because that is all there is.
    assert titles == {"Ready Doc", "Pending Doc"}


async def test_documents_limit_and_offset(api_client):
    """Asserting only a count here would pass even if `offset` were
    silently ignored -- a `limit: 1` query always returns exactly one row
    regardless. Pinning the actual title (the second-newest, since
    `list_documents` orders by `created_at` descending) is what a broken
    `offset` -- or one that a future refactor drops -- would actually flip.
    """
    token = await _register(api_client, "gqlpage@example.com", "Ada Motors GQL Page")
    for i in range(3):
        await _upload(api_client, token, content=f"paged {i}".encode(), title=f"Doc {i}")

    response = await graphql(
        api_client, "{ documents(limit: 1, offset: 1) { title } }", headers=_auth(token)
    )
    docs = response.json()["data"]["documents"]
    assert [d["title"] for d in docs] == ["Doc 1"]


async def test_documents_clamps_a_negative_offset_and_an_oversized_limit(api_client):
    """A negative `offset` reaches Postgres as a literal `OFFSET -1`, which
    Postgres rejects outright -- without clamping, this is an unhandled
    500, not a graceful empty/first-page result. An unclamped `limit` is a
    full-table read one query away. Both must be clamped in
    `DocumentService.list_documents` before the query ever runs."""
    token = await _register(api_client, "gqlclamp@example.com", "Ada Motors GQL Clamp")
    await _upload(api_client, token, content=b"clamp content", title="Clamped Doc")

    response = await graphql(
        api_client,
        "{ documents(limit: 1000000, offset: -5) { title } }",
        headers=_auth(token),
    )
    body = response.json()
    assert body.get("errors") is None, body
    assert [d["title"] for d in body["data"]["documents"]] == ["Clamped Doc"]


# ---------------------------------------------------------------------------
# document(id): nullable, cross-tenant returns null
# ---------------------------------------------------------------------------


async def test_document_requires_authentication(api_client):
    response = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { title } }",
        {"id": str(uuid.uuid4())},
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_document_returns_the_callers_own_document(api_client):
    token = await _register(api_client, "gqlone@example.com", "Ada Motors GQL One")
    document_id = await _upload(api_client, token, content=b"solo content", title="Solo Doc")

    response = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { title chunkCount } }",
        {"id": str(document_id)},
        headers=_auth(token),
    )
    assert response.json()["data"]["document"] == {"title": "Solo Doc", "chunkCount": 0}


async def test_document_for_a_nonexistent_id_returns_null(api_client):
    token = await _register(api_client, "gqlmiss@example.com", "Ada Motors GQL Miss")
    response = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { title } }",
        {"id": str(uuid.uuid4())},
        headers=_auth(token),
    )
    body = response.json()
    assert body.get("errors") is None
    assert body["data"]["document"] is None


async def test_org_b_cannot_fetch_org_as_document(api_client):
    token_a = await _register(api_client, "gqlfa@example.com", "Ada Motors GQL FetchA")
    token_b = await _register(api_client, "gqlfb@example.com", "Ada Motors GQL FetchB")
    document_id = await _upload(api_client, token_a, content=b"secret content", title="Secret Doc")

    response = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { title } }",
        {"id": str(document_id)},
        headers=_auth(token_b),
    )
    body = response.json()
    # Same shape as a nonexistent id, not a distinguishable error -- org B
    # must not be able to tell "not yours" apart from "does not exist".
    assert body.get("errors") is None
    assert body["data"]["document"] is None


# ---------------------------------------------------------------------------
# deleteDocument: cross-tenant raises, own document deletes
# ---------------------------------------------------------------------------


async def test_delete_document_requires_authentication(api_client):
    response = await graphql(
        api_client,
        "mutation M($id: UUID!) { deleteDocument(id: $id) }",
        {"id": str(uuid.uuid4())},
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_delete_document_removes_the_callers_own_document(api_client):
    token = await _register(api_client, "gqldel@example.com", "Ada Motors GQL Del")
    document_id = await _upload(api_client, token, content=b"deleteme", title="Delete Me")

    response = await graphql(
        api_client,
        "mutation M($id: UUID!) { deleteDocument(id: $id) }",
        {"id": str(document_id)},
        headers=_auth(token),
    )
    assert response.json()["data"]["deleteDocument"] is True

    after = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { title } }",
        {"id": str(document_id)},
        headers=_auth(token),
    )
    assert after.json()["data"]["document"] is None


async def test_delete_document_removes_the_stored_file(api_client):
    """`DocumentService.delete` used to remove only the row -- chunks
    cascade via the FK, but the uploaded plaintext at
    `{upload_dir}/{org_id}/{document_id}` had nothing pointed at it and
    was never cleaned up. Checks the real filesystem path
    `store_document_bytes`/`delete_document_bytes` actually use, not just
    that the GraphQL/DB side looks deleted.
    """
    from pathlib import Path

    from app.core.config import get_settings

    token = await _register(api_client, "gqlfile@example.com", "Ada Motors GQL File")
    org_id = await _organization_id(api_client, token)
    document_id = await _upload(api_client, token, content=b"bytes on disk", title="On Disk Doc")

    path = Path(get_settings().upload_dir) / str(org_id) / str(document_id)
    assert path.is_file(), "the upload should have written real bytes, not just a DB row"

    response = await graphql(
        api_client,
        "mutation M($id: UUID!) { deleteDocument(id: $id) }",
        {"id": str(document_id)},
        headers=_auth(token),
    )
    assert response.json()["data"]["deleteDocument"] is True
    assert not path.exists()


async def test_org_b_cannot_delete_org_as_document(api_client):
    token_a = await _register(api_client, "gqlda@example.com", "Ada Motors GQL DelA")
    token_b = await _register(api_client, "gqldb@example.com", "Ada Motors GQL DelB")
    document_id = await _upload(api_client, token_a, content=b"keep me", title="Keep Me")

    response = await graphql(
        api_client,
        "mutation M($id: UUID!) { deleteDocument(id: $id) }",
        {"id": str(document_id)},
        headers=_auth(token_b),
    )
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "not_found"

    # And the document must still be there for its actual owner.
    still_there = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { title } }",
        {"id": str(document_id)},
        headers=_auth(token_a),
    )
    assert still_there.json()["data"]["document"]["title"] == "Keep Me"


# ---------------------------------------------------------------------------
# chunkCount
# ---------------------------------------------------------------------------


async def test_chunk_count_reflects_replaced_chunks(api_client):
    from app.documents.schemas import ChunkInput

    token = await _register(api_client, "gqlchunks@example.com", "Ada Motors GQL Chunks")
    org_id = await _organization_id(api_client, token)
    document_id = await _upload(api_client, token, content=b"chunked content", title="Chunked Doc")

    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    async with tenant_session(tenant) as session:
        await DocumentService(session, tenant).replace_chunks(
            document_id,
            [
                ChunkInput(
                    content=f"chunk {i}",
                    token_count=2,
                    embedding=[0.0] * 1536,
                    embedding_model="hashing",
                )
                for i in range(2)
            ],
        )

    response = await graphql(
        api_client,
        "query Q($id: UUID!) { document(id: $id) { chunkCount } }",
        {"id": str(document_id)},
        headers=_auth(token),
    )
    assert response.json()["data"]["document"]["chunkCount"] == 2

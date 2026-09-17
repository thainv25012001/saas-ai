"""`POST /api/v1/documents` (multipart upload) and
`POST /api/v1/documents/{id}/retry`.

Every test here overrides `documents_api.enqueue_ingest` with a recorder
before making any request through it -- the production function opens a
real Redis connection pool (`app/rag/queue.py`), and no test in this suite
may touch the network. Patching the module-level name (rather than FastAPI's
`dependency_overrides`) works with any client fixture, since the endpoint
calls `enqueue_ingest` as a bare module global, resolved fresh on every call.
"""

import asyncio
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

DOCUMENTS_URL = "/api/v1/documents"


class _RecordingQueue:
    """Fake `enqueue_ingest`: records every call instead of touching Redis."""

    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, document_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        self.calls.append((document_id, organization_id))


@pytest.fixture
def queue(monkeypatch) -> _RecordingQueue:
    recorder = _RecordingQueue()
    monkeypatch.setattr(documents_api, "enqueue_ingest", recorder)
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
            "full_name": "Docs Owner",
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
    content: bytes = b"hello world",
    content_type: str = "text/plain",
    filename: str = "doc.txt",
    title: str | None = None,
):
    data = {"title": title} if title else {}
    files = {"file": (filename, content, content_type)}
    return await api_client.post(DOCUMENTS_URL, headers=_auth(token), files=files, data=data)


async def _set_status(org_id: uuid.UUID, document_id: uuid.UUID, status: str) -> None:
    tenant = _tenant(org_id)
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
# 1. Upload success + enqueue
# ---------------------------------------------------------------------------


async def test_upload_returns_202_and_a_pending_document(api_client, clean_users, queue):
    token = await _register(api_client, "up1@example.com", "Ada Motors Documents Up1")
    response = await _upload(api_client, token, title="Refund policy")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["title"] == "Refund policy"
    assert body["mime_type"] == "text/plain"


async def test_upload_enqueues_exactly_one_job(api_client, clean_users, queue):
    token = await _register(api_client, "up2@example.com", "Ada Motors Documents Up2")
    org_id = await _organization_id(api_client, token)
    response = await _upload(api_client, token)
    document_id = uuid.UUID(response.json()["id"])

    assert queue.calls == [(document_id, org_id)]


# ---------------------------------------------------------------------------
# 2. Dedup: identical bytes must not re-enqueue
# ---------------------------------------------------------------------------


async def test_reuploading_identical_bytes_returns_the_same_document_and_does_not_reenqueue(
    api_client, clean_users, queue
):
    token = await _register(api_client, "dup@example.com", "Ada Motors Documents Dup")
    first = await _upload(api_client, token, content=b"identical payload")
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]

    second = await _upload(api_client, token, content=b"identical payload")
    assert second.status_code == 202, second.text
    assert second.json()["id"] == first_id

    # Exactly one enqueue call total -- the second upload must not add a
    # second one. This is the assertion a broken dedup guard (one that
    # returns the existing row but still calls `enqueue`) would flip to
    # len(queue.calls) == 2.
    assert len(queue.calls) == 1


async def test_reuploading_different_bytes_creates_a_second_document_and_enqueues_again(
    api_client, clean_users, queue
):
    """The inverse of the dedup test above: proves dedup keys off content,
    not off "have we seen this org upload before" -- a second, genuinely
    different file must still get its own row and its own job."""
    token = await _register(api_client, "dup2@example.com", "Ada Motors Documents Dup2")
    first = await _upload(api_client, token, content=b"payload one")
    second = await _upload(api_client, token, content=b"payload two, not the same")

    assert first.json()["id"] != second.json()["id"]
    assert len(queue.calls) == 2


# ---------------------------------------------------------------------------
# 3. Unsupported mime type
# ---------------------------------------------------------------------------


async def test_unsupported_mime_type_is_422(api_client, clean_users, queue):
    token = await _register(api_client, "mime@example.com", "Ada Motors Documents Mime")
    response = await _upload(
        api_client,
        token,
        content=b"PK\x03\x04 not really a zip but irrelevant",
        content_type="application/zip",
        filename="archive.zip",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unsupported_document_type"
    assert queue.calls == []


# ---------------------------------------------------------------------------
# 4. Size limit -- both layers, pinned separately
# ---------------------------------------------------------------------------
#
# `test_upload_over_the_limit_is_413` below (kept from round 1) proves only
# that *a* 413 happens somewhere -- it sets a tiny ceiling and posts a body
# over it with an accurate Content-Length, which trips whichever of the two
# layers in `upload_document` survives; deleting either one in isolation
# still leaves that test green. The two tests after it each isolate one
# layer instead: `test_content_length_precheck_fires_without_reading_the_body`
# proves the header-only rejection specifically (a tiny real body, declared
# huge), and `test_stream_cap_fires_for_a_chunked_request_with_no_content_length`
# proves the mid-stream cap specifically (no Content-Length header at all,
# so the precheck cannot fire, and Starlette's own multipart parser has no
# size ceiling for a file part -- see `_capped_receive`'s docstring).


@pytest.fixture
def set_upload_limit(monkeypatch):
    """A factory so each test picks its own ceiling."""
    from app.core.config import get_settings

    def _set(max_request_bytes: int):
        settings = get_settings().model_copy(update={"max_request_bytes": max_request_bytes})
        monkeypatch.setattr(documents_api, "get_settings", lambda: settings)
        return settings

    return _set


async def test_upload_over_the_limit_is_413(api_client, clean_users, queue, set_upload_limit):
    set_upload_limit(10)
    token = await _register(api_client, "big@example.com", "Ada Motors Documents Big")
    response = await _upload(api_client, token, content=b"this is definitely more than 10 bytes")
    assert response.status_code == 413, response.text
    assert queue.calls == []


async def test_upload_at_or_under_the_limit_still_succeeds(
    api_client, clean_users, queue, set_upload_limit
):
    """Proves the 413 above is actually about size, not a blanket rejection
    -- a payload that fits under a (still lowered, but multipart-overhead
    aware) ceiling must still go through as 202."""
    set_upload_limit(1024)
    token = await _register(api_client, "small@example.com", "Ada Motors Documents Small")
    response = await _upload(api_client, token, content=b"tiny")
    assert response.status_code == 202, response.text


async def test_content_length_precheck_fires_without_reading_the_body(
    api_client, clean_users, queue, set_upload_limit
):
    """Declares a 1 GB Content-Length against an actually-tiny body. Only
    the header-based precheck can catch this: the real body is 4 bytes, so
    if the precheck did not run at all, `request.form()` would parse the
    (tiny) body just fine and the request would succeed as 202. httpx
    honours an explicit `content-length` override even when it doesn't
    match the real body -- confirmed separately -- which is what lets this
    test lie about size the same way an adversarial client would."""
    set_upload_limit(20 * 1024 * 1024)
    token = await _register(api_client, "preclen@example.com", "Ada Motors Documents PreCLen")
    files = {"file": ("doc.txt", b"tiny", "text/plain")}
    response = await api_client.post(
        DOCUMENTS_URL,
        headers={**_auth(token), "content-length": str(1024**3)},
        files=files,
    )
    assert response.status_code == 413, response.text
    assert queue.calls == []


def _multipart_body(boundary: str, file_content: bytes) -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="big.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode()
        + file_content
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def _chunked(body: bytes, chunk_size: int = 256):
    """An async generator body forces httpx to send chunked transfer
    encoding with no `Content-Length` header at all -- confirmed
    separately against a probe ASGI app. That is the one shape the
    Content-Length precheck cannot see, which is exactly what this needs
    to isolate the stream cap from it.
    """
    for start in range(0, len(body), chunk_size):
        yield body[start : start + chunk_size]
        await asyncio.sleep(0)


async def test_stream_cap_fires_for_a_chunked_request_with_no_content_length(
    api_client, clean_users, queue, set_upload_limit
):
    set_upload_limit(200)
    token = await _register(api_client, "chunked@example.com", "Ada Motors Documents Chunked")
    boundary = "task5streamcapboundary"
    body = _multipart_body(boundary, b"x" * 5000)

    # No `content-length` header at all -- `content=<async generator>`
    # is what forces httpx into chunked transfer encoding in the first
    # place (confirmed separately against a probe ASGI app).
    response = await api_client.post(
        DOCUMENTS_URL,
        headers={**_auth(token), "content-type": f"multipart/form-data; boundary={boundary}"},
        content=_chunked(body),
    )
    assert response.status_code == 413, response.text
    assert queue.calls == []


# ---------------------------------------------------------------------------
# 5. Auth and cross-tenant
# ---------------------------------------------------------------------------


async def test_upload_without_a_token_is_401(api_client, clean_users, queue):
    files = {"file": ("doc.txt", b"hello", "text/plain")}
    response = await api_client.post(DOCUMENTS_URL, files=files)
    assert response.status_code == 401


async def test_retry_without_a_token_is_401(api_client, clean_users, queue):
    response = await api_client.post(f"{DOCUMENTS_URL}/{uuid.uuid4()}/retry")
    assert response.status_code == 401


async def test_org_b_cannot_retry_org_as_document(api_client, clean_users, queue):
    token_a = await _register(api_client, "isoa@example.com", "Ada Motors Documents IsoA")
    org_a = await _organization_id(api_client, token_a)
    uploaded = await _upload(api_client, token_a)
    document_id = uploaded.json()["id"]
    await _set_status(org_a, uuid.UUID(document_id), "failed")

    token_b = await _register(api_client, "isob@example.com", "Ada Motors Documents IsoB")
    response = await api_client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=_auth(token_b))

    # Org B never learns the document exists at all -- same NotFoundError a
    # nonexistent id gets, not a 403 that would confirm the id is real.
    assert response.status_code == 404
    assert queue.calls == [(uuid.UUID(document_id), org_a)]  # only the original upload


# ---------------------------------------------------------------------------
# 6. Retry semantics
# ---------------------------------------------------------------------------


async def test_retry_on_a_ready_document_is_rejected(api_client, clean_users, queue):
    token = await _register(api_client, "ready@example.com", "Ada Motors Documents Ready")
    org_id = await _organization_id(api_client, token)
    uploaded = await _upload(api_client, token)
    document_id = uuid.UUID(uploaded.json()["id"])
    await _set_status(org_id, document_id, "processing")
    await _set_status(org_id, document_id, "ready")

    response = await api_client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=_auth(token))
    assert response.status_code == 409, response.text
    # Only the original upload enqueued -- retry on `ready` must not add one.
    assert queue.calls == [(document_id, org_id)]


async def test_retry_on_a_failed_document_reenqueues(api_client, clean_users, queue):
    token = await _register(api_client, "failed@example.com", "Ada Motors Documents Failed")
    org_id = await _organization_id(api_client, token)
    uploaded = await _upload(api_client, token)
    document_id = uuid.UUID(uploaded.json()["id"])
    await _set_status(org_id, document_id, "failed")

    response = await api_client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=_auth(token))
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "failed"  # unchanged until the worker picks it up
    assert queue.calls == [(document_id, org_id), (document_id, org_id)]


async def test_retry_on_a_pending_document_reenqueues(api_client, clean_users, queue):
    """Round 1 rejected `pending` on the theory that it already has a job
    in flight. That premise doesn't hold: `enqueue_ingest` runs *after*
    the upload's own commit, so any failure in that call (Redis down, the
    process dying between commit and enqueue) leaves a `pending` row with
    no job anywhere -- and, before this test, no way back either: retry
    was rejected and re-uploading identical bytes hits checksum dedup and
    enqueues nothing. `pending` is retryable now; see the comment on
    `retry_document` for why re-enqueueing a document that in fact already
    has a job in flight is still safe (`replace_chunks` deletes before
    inserting, so a duplicate job replaces rather than doubles the
    corpus)."""
    token = await _register(api_client, "pending@example.com", "Ada Motors Documents Pending")
    org_id = await _organization_id(api_client, token)
    uploaded = await _upload(api_client, token)
    document_id = uuid.UUID(uploaded.json()["id"])

    response = await api_client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=_auth(token))
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "pending"
    assert queue.calls == [(document_id, org_id), (document_id, org_id)]


async def test_retry_on_a_processing_document_is_rejected(api_client, clean_users, queue):
    token = await _register(api_client, "proc@example.com", "Ada Motors Documents Proc")
    org_id = await _organization_id(api_client, token)
    uploaded = await _upload(api_client, token)
    document_id = uuid.UUID(uploaded.json()["id"])
    await _set_status(org_id, document_id, "processing")

    response = await api_client.post(f"{DOCUMENTS_URL}/{document_id}/retry", headers=_auth(token))
    assert response.status_code == 409, response.text
    assert queue.calls == [(document_id, org_id)]


async def test_retry_on_a_nonexistent_document_is_404(api_client, clean_users, queue):
    token = await _register(api_client, "none@example.com", "Ada Motors Documents None")
    response = await api_client.post(f"{DOCUMENTS_URL}/{uuid.uuid4()}/retry", headers=_auth(token))
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 7. Validation edge cases: no file part, title fallback, title truncation
# ---------------------------------------------------------------------------


async def test_upload_without_a_file_part_is_422(api_client, clean_users, queue):
    token = await _register(api_client, "nofile@example.com", "Ada Motors Documents NoFile")
    response = await api_client.post(
        DOCUMENTS_URL, headers=_auth(token), data={"title": "No file here"}
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_input"
    assert queue.calls == []


async def test_upload_without_a_title_falls_back_to_the_filename(api_client, clean_users, queue):
    token = await _register(api_client, "notitle@example.com", "Ada Motors Documents NoTitle")
    response = await _upload(api_client, token, filename="quarterly-report.txt", title=None)
    assert response.status_code == 202, response.text
    assert response.json()["title"] == "quarterly-report.txt"


async def test_upload_title_longer_than_255_chars_is_truncated(api_client, clean_users, queue):
    token = await _register(api_client, "longtitle@example.com", "Ada Motors Documents LongTitle")
    long_title = "x" * 400
    response = await _upload(api_client, token, title=long_title)
    assert response.status_code == 202, response.text
    assert response.json()["title"] == "x" * 255

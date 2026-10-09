from __future__ import annotations

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, HTTPException, Request, Response, status
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.logging import get_logger
from app.gmail.pubsub import (
    PubSubAuthError,
    PubSubPayloadError,
    decode_pubsub_envelope,
    gmail_sync_job_id,
    verify_pubsub_request,
)

router = APIRouter(prefix="/api/v1/gmail", tags=["gmail-pubsub"])
logger = get_logger(__name__)
_MAX_PUSH_BODY_BYTES = 64 * 1024


async def enqueue_gmail_sync(mailbox_email: str, history_id: str) -> bool:
    from app.workers.tasks import sync_gmail_mailbox_task

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        job = await pool.enqueue_job(
            sync_gmail_mailbox_task.__name__,
            mailbox_email=mailbox_email,
            notification_history_id=history_id,
            _job_id=gmail_sync_job_id(mailbox_email, history_id),
        )
        # arq returns None when the _job_id already exists; the existing job owns this event.
        return job is not None
    finally:
        await pool.aclose()


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _MAX_PUSH_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Pub/Sub push payload is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_PUSH_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Pub/Sub push payload is too large")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/pubsub", status_code=status.HTTP_204_NO_CONTENT)
async def gmail_pubsub_push(request: Request) -> Response:
    settings = get_settings()
    if (
        not settings.gmail_pubsub_push_audience
        or not settings.gmail_pubsub_expected_service_account
    ):
        logger.warning("gmail_pubsub_push_rejected", reason="pubsub_auth_not_configured")
        raise HTTPException(status_code=503, detail="Pub/Sub authentication is not configured")
    try:
        await run_in_threadpool(
            verify_pubsub_request,
            request.headers.get("authorization"),
            expected_audience=settings.gmail_pubsub_push_audience,
            expected_email=settings.gmail_pubsub_expected_service_account,
        )
    except PubSubAuthError as exc:
        logger.warning("gmail_pubsub_push_rejected", reason="auth_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Pub/Sub authentication failed") from exc

    raw_body = await _read_bounded_body(request)
    try:
        message = decode_pubsub_envelope(raw_body)
    except PubSubPayloadError as exc:
        logger.warning("gmail_pubsub_push_rejected", reason="invalid_payload", error=str(exc))
        raise HTTPException(status_code=400, detail="Pub/Sub push payload is invalid") from exc
    logger.info(
        "gmail_pubsub_push_received",
        mailbox_email=message.mailbox_email,
        history_id=message.history_id,
    )
    try:
        queued = await enqueue_gmail_sync(message.mailbox_email, message.history_id)
    except Exception as exc:
        logger.warning("gmail_sync_enqueue_failed", error=f"{type(exc).__name__}: {exc}")
        # A non-2xx tells Pub/Sub to retry. Do not acknowledge work that was not queued.
        raise HTTPException(status_code=503, detail="Gmail sync job could not be queued") from exc
    # queued=False: a job for this exact notification already exists.
    logger.info("gmail_sync_enqueued", history_id=message.history_id, queued=queued)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

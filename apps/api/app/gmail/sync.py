from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any

from arq import Retry
from pydantic import BaseModel, ConfigDict, Field
from redis.exceptions import LockError
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.tenancy import TenantContext, tenant_session, untenanted_session
from app.db.models import (
    GmailConnection,
    Product,
    Supplier,
    SupplierMessage,
    SupplierQuote,
    SupplierReport,
)
from app.db.models.supplier import SupplierMapping
from app.gmail.client import GmailAPIError, GmailClient
from app.gmail.oauth import GmailOAuthClient
from app.gmail.token_crypto import decrypt_token, encrypt_token
from app.supplier_quotes.analysis import (
    SupplierOffer,
    compare_product_offers,
    group_offers_by_product,
    normalize_product_name,
)
from app.supplier_quotes.classification import (
    ProductMatchResult,
    classify_supplier_email,
    deterministic_product_match,
    suggest_mapping,
    suggest_product_match,
)
from app.supplier_quotes.schemas import (
    CanonicalQuoteRow,
    WorkbookMapping,
    WorkbookRowIssue,
)
from app.supplier_quotes.service import SupplierQuoteService
from app.supplier_quotes.workbooks import inspect_supplier_workbook, parse_supplier_workbook

_MAX_HISTORY_PAGES = 100
_MAX_RECONCILIATION_PAGES = 100
_MAILBOX_LOCK_TTL_SECONDS = 900
_PRODUCT_MATCH_CANDIDATES = 30
_MAX_PROMPT_TEXT = 12_000
#: Rows per message that may ask the LLM for a catalogue match. Exact SKU and
#: remembered matches need no call; past this, rows wait for review rather
#: than turning one large price list into thousands of sequential LLM calls.
_LLM_MATCHES_PER_MESSAGE = 25
_BUSY_RETRY_SECONDS = 30
_TRANSIENT_RETRY_SECONDS = 60


class GmailSyncError(RuntimeError):
    pass


class HistoryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_ids: list[str]
    history_id: str


class GmailAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(min_length=1, max_length=255)
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str | None = None
    size: int | None = Field(default=None, ge=0)
    inline_data: str | None = None


async def fetch_history_batch(
    gmail: GmailClient,
    access_token: str,
    start_history_id: str,
) -> HistoryBatch:
    message_ids: list[str] = []
    seen: set[str] = set()
    history_cursor = start_history_id
    page_token: str | None = None
    for _ in range(_MAX_HISTORY_PAGES):
        body = await gmail.list_history(
            access_token,
            start_history_id,
            page_token=page_token,
            max_results=500,
        )
        page_cursor = body.get("historyId")
        if isinstance(page_cursor, str) and _history_id_value(page_cursor) >= _history_id_value(
            history_cursor
        ):
            history_cursor = page_cursor
        history = body.get("history", [])
        if isinstance(history, list):
            for record in history:
                if not isinstance(record, dict):
                    continue
                record_cursor = record.get("id")
                if isinstance(record_cursor, str) and _history_id_value(
                    record_cursor
                ) >= _history_id_value(history_cursor):
                    history_cursor = record_cursor
                added = record.get("messagesAdded", [])
                if not isinstance(added, list):
                    continue
                for change in added:
                    message = change.get("message") if isinstance(change, dict) else None
                    message_id = message.get("id") if isinstance(message, dict) else None
                    if isinstance(message_id, str) and message_id and message_id not in seen:
                        seen.add(message_id)
                        message_ids.append(message_id)
        raw_page_token = body.get("nextPageToken")
        page_token = raw_page_token if isinstance(raw_page_token, str) else None
        if not page_token:
            return HistoryBatch(message_ids=message_ids, history_id=history_cursor)
    raise GmailSyncError("Gmail history pagination exceeded the configured page limit")


def notification_is_behind_cursor(notification_id: str, cursor_id: str) -> bool:
    if not notification_id.isdigit() or not cursor_id.isdigit():
        return False
    return int(notification_id) < int(cursor_id)


def extract_xlsx_attachments(message: dict[str, Any]) -> list[GmailAttachment]:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return []
    found: list[GmailAttachment] = []

    def visit(part: dict[str, Any]) -> None:
        filename = part.get("filename")
        mime_type = part.get("mimeType")
        body = part.get("body")
        if not isinstance(body, dict):
            body = {}
        part_id = part.get("partId")
        attachment_id = body.get("attachmentId")
        inline_data = body.get("data")
        is_xlsx = (
            isinstance(filename, str) and filename.strip().casefold().endswith(".xlsx")
        ) or mime_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if is_xlsx:
            normalized_filename = (
                filename.strip()[:255]
                if isinstance(filename, str) and filename.strip()
                else f"attachment-{part_id or len(found)}.xlsx"
            )
            if isinstance(attachment_id, str) and attachment_id:
                found.append(
                    GmailAttachment(
                        attachment_id=attachment_id,
                        filename=normalized_filename,
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                        size=body.get("size") if isinstance(body.get("size"), int) else None,
                    )
                )
            elif isinstance(inline_data, str) and isinstance(part_id, (str, int)):
                found.append(
                    GmailAttachment(
                        attachment_id=f"inline:{part_id}",
                        filename=normalized_filename,
                        mime_type=mime_type if isinstance(mime_type, str) else None,
                        size=body.get("size") if isinstance(body.get("size"), int) else None,
                        inline_data=inline_data,
                    )
                )
        parts = part.get("parts")
        if isinstance(parts, list):
            for nested in parts:
                if isinstance(nested, dict):
                    visit(nested)

    visit(payload)
    return found


async def fetch_attachment_bytes(
    gmail: GmailClient,
    access_token: str,
    message_id: str,
    attachment: GmailAttachment,
) -> bytes:
    if attachment.inline_data is None:
        return await gmail.get_attachment(access_token, message_id, attachment.attachment_id)
    encoded = attachment.inline_data
    max_bytes = gmail.max_attachment_bytes
    max_encoded = ((max_bytes + 2) // 3) * 4 + 4
    if len(encoded) > max_encoded or (attachment.size is not None and attachment.size > max_bytes):
        raise GmailSyncError("Gmail attachment exceeds the configured size limit")
    try:
        data = base64.urlsafe_b64decode(encoded + "===")
    except (ValueError, binascii.Error) as exc:
        raise GmailSyncError("Gmail returned an invalid attachment") from exc
    if len(data) > max_bytes:
        raise GmailSyncError("Gmail attachment exceeds the configured size limit")
    return data


async def fetch_attachment_if_available(
    gmail: GmailClient,
    access_token: str,
    message_id: str,
    attachment: GmailAttachment,
) -> bytes | None:
    try:
        return await fetch_attachment_bytes(gmail, access_token, message_id, attachment)
    except GmailAPIError as exc:
        if exc.status_code == 404:
            return None
        raise


def _history_id_value(value: str) -> int:
    return int(value) if value.isdigit() else 0


def _is_transient(exc: GmailAPIError) -> bool:
    """Rate limits and Google-side failures: worth retrying the whole job."""
    return exc.status_code is not None and (exc.status_code == 429 or exc.status_code >= 500)


async def run_gmail_sync(
    *,
    mailbox_email: str,
    notification_history_id: str | None,
    force_sync: bool = False,
) -> None:
    normalized_email = mailbox_email.strip().casefold()
    async with untenanted_session() as session:
        result = await session.execute(
            text("SELECT organization_id, connection_id FROM resolve_gmail_mailboxes(:email)"),
            {"email": normalized_email},
        )
        resolved = [(uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))) for row in result.all()]

    busy = False
    for organization_id, connection_id in resolved:
        lock = get_redis().lock(
            f"gmail:sync:{connection_id}",
            timeout=_MAILBOX_LOCK_TTL_SECONDS,
            blocking_timeout=0,
        )
        if not await lock.acquire(blocking=False):
            # Another job holds this mailbox and may have listed its history
            # before the change this notification announces, so skipping
            # would strand that mail until the next notification. Retry.
            busy = True
            continue
        try:
            await _sync_connection(
                organization_id=organization_id,
                connection_id=connection_id,
                notification_history_id=notification_history_id,
                force_sync=force_sync,
            )
        finally:
            try:
                await lock.release()
            except LockError:
                pass
    if busy:
        raise Retry(defer=_BUSY_RETRY_SECONDS)


async def run_gmail_message_reprocess(
    *,
    organization_id: str,
    supplier_message_id: str,
    mapping_id: str,
) -> None:
    """Re-fetch one reviewed Gmail message after its layout is confirmed."""
    try:
        org_id = uuid.UUID(organization_id)
        message_id = uuid.UUID(supplier_message_id)
        confirmed_mapping_id = uuid.UUID(mapping_id)
    except ValueError as exc:
        raise GmailSyncError("supplier message reprocess request is invalid") from exc
    tenant = _worker_tenant(org_id, f"gmail-reprocess:{message_id}")
    async with tenant_session(tenant) as session:
        message_result = await session.execute(
            select(SupplierMessage).where(
                SupplierMessage.id == message_id,
                SupplierMessage.organization_id == org_id,
            )
        )
        supplier_message = message_result.scalar_one_or_none()
        mapping_result = await session.execute(
            select(SupplierMapping).where(
                SupplierMapping.id == confirmed_mapping_id,
                SupplierMapping.organization_id == org_id,
                SupplierMapping.is_confirmed.is_(True),
                SupplierMapping.source_message_id == message_id,
            )
        )
        mapping = mapping_result.scalar_one_or_none()
        if supplier_message is None or mapping is None:
            return
        connection_result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == supplier_message.gmail_connection_id,
                GmailConnection.organization_id == org_id,
            )
        )
        connection = connection_result.scalar_one_or_none()
        if connection is None or connection.status != "connected":
            supplier_message.status = "failed"
            supplier_message.error_code = "gmail_connection_unavailable"
            supplier_message.processed_at = datetime.now(UTC)
            return
        connection_id = connection.id
        mailbox_email = connection.email
        refresh_ciphertext = connection.refresh_token_ciphertext
        history_id = supplier_message.history_id or connection.history_id or "0"
        gmail_message_id = supplier_message.gmail_message_id

    lock = get_redis().lock(
        f"gmail:sync:{connection_id}",
        timeout=_MAILBOX_LOCK_TTL_SECONDS,
        blocking_timeout=0,
    )
    if not await lock.acquire(blocking=False):
        raise Retry(defer=_BUSY_RETRY_SECONDS)
    try:
        try:
            refresh_token = decrypt_token(refresh_ciphertext)
            tokens = await GmailOAuthClient().refresh_access_token(refresh_token)
        except (RuntimeError, ValueError):
            await _mark_connection_error(tenant, connection_id, "gmail_refresh_failed")
            return
        if tokens.refresh_token and tokens.refresh_token != refresh_token:
            async with tenant_session(tenant) as session:
                result = await session.execute(
                    select(GmailConnection).where(
                        GmailConnection.id == connection_id,
                        GmailConnection.organization_id == org_id,
                    )
                )
                connection = result.scalar_one_or_none()
                if connection is not None:
                    connection.refresh_token_ciphertext = encrypt_token(tokens.refresh_token)
        gmail = GmailClient()
        message = await fetch_message_if_available(gmail, tokens.access_token, gmail_message_id)
        if message is None:
            async with tenant_session(tenant) as session:
                service = SupplierQuoteService(session, tenant)
                await service.record_processing_status(
                    message_id,
                    status="review_required",
                    error_code="gmail_message_unavailable",
                )
            return
        try:
            await _process_isolated(
                tenant=tenant,
                connection_id=connection_id,
                mailbox_email=mailbox_email,
                history_id=history_id,
                message_id=gmail_message_id,
                message=message,
                gmail=gmail,
                access_token=tokens.access_token,
                force_reprocess=True,
            )
        except GmailAPIError as exc:
            if exc.status_code == 401:
                await _mark_connection_error(tenant, connection_id, "gmail_access_denied")
                return
            raise Retry(defer=_TRANSIENT_RETRY_SECONDS) from exc
    finally:
        try:
            await lock.release()
        except LockError:
            pass


async def run_gmail_maintenance() -> None:
    """Renew watches that expire soon, then reconcile each mailbox's history cursor."""
    async with untenanted_session() as session:
        result = await session.execute(
            text(
                "SELECT organization_id, connection_id, email, history_id "
                "FROM list_gmail_connections_for_maintenance()"
            )
        )
        mailboxes = [
            (uuid.UUID(str(row[0])), uuid.UUID(str(row[1])), str(row[2]), row[3])
            for row in result.all()
        ]

    now = datetime.now(UTC)
    for organization_id, connection_id, email, cursor in mailboxes:
        tenant = _worker_tenant(organization_id, f"gmail-watch:{connection_id}")
        try:
            await _maintain_mailbox(tenant, connection_id, email, cursor, now)
        except Retry:
            # Another job is syncing this mailbox right now; it is current.
            continue
        except Exception:
            # One tenant's mailbox must not stop renewal for every mailbox
            # after it in the list. Surface it on that connection instead.
            await _mark_connection_error(tenant, connection_id, "gmail_maintenance_failed")


async def _maintain_mailbox(
    tenant: TenantContext,
    connection_id: uuid.UUID,
    email: str,
    cursor: str | None,
    now: datetime,
) -> None:
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == connection_id,
                GmailConnection.organization_id == tenant.organization_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is None:
            return
        refresh_ciphertext = connection.refresh_token_ciphertext
        expires_at = connection.watch_expires_at
        history_id = connection.history_id
    if expires_at is None or expires_at <= now + timedelta(days=1):
        await _renew_watch(tenant, connection_id, refresh_ciphertext)
    if history_id:
        await run_gmail_sync(
            mailbox_email=email,
            notification_history_id=cursor,
            force_sync=True,
        )


async def _renew_watch(
    tenant: TenantContext,
    connection_id: uuid.UUID,
    refresh_token_ciphertext: str,
) -> None:
    try:
        refresh_token = decrypt_token(refresh_token_ciphertext)
        tokens = await GmailOAuthClient().refresh_access_token(refresh_token)
    except (RuntimeError, ValueError):
        await _mark_connection_error(tenant, connection_id, "gmail_refresh_failed")
        return
    topic = get_settings().gmail_pubsub_topic
    if not topic:
        await _mark_connection_error(tenant, connection_id, "gmail_topic_not_configured")
        return
    try:
        watch = await GmailClient().watch(tokens.access_token, topic)
    except RuntimeError:
        await _mark_connection_error(tenant, connection_id, "gmail_watch_renewal_failed")
        return
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == connection_id,
                GmailConnection.organization_id == tenant.organization_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is not None:
            if tokens.refresh_token:
                connection.refresh_token_ciphertext = encrypt_token(tokens.refresh_token)
            connection.history_id = connection.history_id or watch.history_id
            connection.watch_expires_at = watch.expiration
            connection.status = "connected"
            connection.last_error_code = None


async def _sync_connection(
    *,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    notification_history_id: str | None,
    force_sync: bool,
) -> None:
    tenant = _worker_tenant(organization_id, f"gmail-sync:{connection_id}")
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == connection_id,
                GmailConnection.organization_id == organization_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is None or connection.status != "connected":
            return
        cursor = connection.history_id
        last_sync = connection.last_history_sync_at or connection.created_at
        refresh_ciphertext = connection.refresh_token_ciphertext
        mailbox_email = connection.email

    if not cursor:
        await _mark_connection_error(tenant, connection_id, "missing_history_cursor")
        return
    if (
        not force_sync
        and notification_history_id is not None
        and _history_id_value(notification_history_id) <= _history_id_value(cursor)
    ):
        return

    try:
        refresh_token = decrypt_token(refresh_ciphertext)
        tokens = await GmailOAuthClient().refresh_access_token(refresh_token)
    except (RuntimeError, ValueError):
        await _mark_connection_error(tenant, connection_id, "gmail_refresh_failed")
        return
    if tokens.refresh_token and tokens.refresh_token != refresh_token:
        async with tenant_session(tenant) as session:
            result = await session.execute(
                select(GmailConnection).where(
                    GmailConnection.id == connection_id,
                    GmailConnection.organization_id == organization_id,
                )
            )
            connection = result.scalar_one_or_none()
            if connection is not None:
                connection.refresh_token_ciphertext = encrypt_token(tokens.refresh_token)

    gmail = GmailClient()
    try:
        batch = await fetch_history_batch(gmail, tokens.access_token, cursor)
        message_ids = batch.message_ids
        next_cursor = batch.history_id
    except GmailAPIError as exc:
        if _is_transient(exc):
            raise Retry(defer=_TRANSIENT_RETRY_SECONDS) from exc
        if exc.status_code != 404 or last_sync is None:
            await _mark_connection_error(
                tenant,
                connection_id,
                "gmail_history_sync_failed" if exc.status_code != 404 else "history_cursor_expired",
            )
            if exc.status_code == 404 and last_sync is None:
                return
            raise
        profile = await gmail.get_profile(tokens.access_token)
        reconciliation_cursor = profile.history_id
        message_ids = await _list_messages_since(gmail, tokens.access_token, last_sync)
        next_cursor = reconciliation_cursor

    for message_id in message_ids:
        try:
            message = await fetch_message_if_available(gmail, tokens.access_token, message_id)
            if message is None or not _is_inbox_message(message):
                continue
            await _process_isolated(
                tenant=tenant,
                connection_id=connection_id,
                mailbox_email=mailbox_email,
                history_id=notification_history_id or next_cursor,
                message_id=message_id,
                message=message,
                gmail=gmail,
                access_token=tokens.access_token,
            )
        except GmailAPIError as exc:
            if exc.status_code == 401:
                await _mark_connection_error(tenant, connection_id, "gmail_access_denied")
                return
            if _is_transient(exc):
                # The cursor stays put; messages already processed are
                # skipped on the retry by their stored status.
                raise Retry(defer=_TRANSIENT_RETRY_SECONDS) from exc
            # Any other refusal to fetch this one message (400, 403) will not
            # change on retry; leave it rather than block the mailbox.
            continue

    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == connection_id,
                GmailConnection.organization_id == organization_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is not None and next_cursor:
            connection.history_id = next_cursor
            connection.last_history_sync_at = datetime.now(UTC)
            connection.last_error_code = None


async def _process_isolated(
    *,
    tenant: TenantContext,
    connection_id: uuid.UUID,
    mailbox_email: str,
    history_id: str,
    message_id: str,
    message: dict[str, Any],
    gmail: GmailClient,
    access_token: str,
    force_reprocess: bool = False,
) -> None:
    """Process one message so that its failure is recorded, not propagated.

    A message that fails for a reason a retry will not change -- a file no
    parser anticipated, a database limit, an LLM outage -- would otherwise
    abort the sync before the cursor advances, and every later sync would
    hit it first: one email permanently stopping the mailbox. Its own
    transaction has rolled back, so the failure is written in a new one.
    Only Gmail auth (401) and transient Gmail errors still propagate: the
    first is the caller's to record, the second is retried as a whole job.
    """
    try:
        await _process_gmail_message(
            tenant=tenant,
            connection_id=connection_id,
            mailbox_email=mailbox_email,
            history_id=history_id,
            message_id=message_id,
            message=message,
            gmail=gmail,
            access_token=access_token,
            force_reprocess=force_reprocess,
        )
    except GmailAPIError as exc:
        if exc.status_code == 401 or _is_transient(exc):
            raise
        await _record_message_failure(tenant, connection_id, history_id, message_id, message)
    except Exception:
        await _record_message_failure(tenant, connection_id, history_id, message_id, message)


async def _record_message_failure(
    tenant: TenantContext,
    connection_id: uuid.UUID,
    history_id: str,
    message_id: str,
    message: dict[str, Any],
) -> None:
    sender_email, _subject, _snippet, received_at = _message_metadata(message)
    async with tenant_session(tenant) as session:
        service = SupplierQuoteService(session, tenant)
        supplier_message, _ = await service.get_or_create_message(
            connection_id=connection_id,
            gmail_message_id=message_id,
            history_id=history_id,
            sender_email=sender_email,
            received_at=received_at,
            attachment_ids=_attachment_identities(extract_xlsx_attachments(message)),
        )
        await service.record_processing_status(
            supplier_message.id, status="failed", error_code="processing_failed"
        )


async def fetch_message_if_available(
    gmail: GmailClient,
    access_token: str,
    message_id: str,
) -> dict[str, object] | None:
    try:
        return await gmail.get_message(access_token, message_id)
    except GmailAPIError as exc:
        if exc.status_code == 404:
            return None
        raise


async def _list_messages_since(
    gmail: GmailClient,
    access_token: str,
    last_sync: datetime,
) -> list[str]:
    after_epoch = max(0, int(last_sync.timestamp()) - 1)
    query = f"in:inbox after:{after_epoch}"
    page_token: str | None = None
    seen: set[str] = set()
    ordered: list[str] = []
    for _ in range(_MAX_RECONCILIATION_PAGES):
        body = await gmail.list_messages(
            access_token,
            query,
            page_token=page_token,
            max_results=500,
        )
        messages = body.get("messages", [])
        if isinstance(messages, list):
            for message in messages:
                message_id = message.get("id") if isinstance(message, dict) else None
                if isinstance(message_id, str) and message_id and message_id not in seen:
                    seen.add(message_id)
                    ordered.append(message_id)
        raw_page_token = body.get("nextPageToken")
        page_token = raw_page_token if isinstance(raw_page_token, str) else None
        if not page_token:
            return ordered
    raise GmailSyncError("Gmail reconciliation exceeded the configured page limit")


def _worker_tenant(organization_id: uuid.UUID, request_id: str) -> TenantContext:
    return TenantContext(
        organization_id=organization_id,
        user_id=None,
        role=None,
        request_id=request_id,
    )


async def _mark_connection_error(
    tenant: TenantContext,
    connection_id: uuid.UUID,
    error_code: str,
) -> None:
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == connection_id,
                GmailConnection.organization_id == tenant.organization_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is not None:
            connection.last_error_code = error_code[:64]
            if error_code in {"gmail_refresh_failed", "gmail_access_denied"}:
                connection.status = "needs_reauth"


def _is_inbox_message(message: dict[str, Any]) -> bool:
    labels = message.get("labelIds", [])
    return isinstance(labels, list) and "INBOX" in labels


def _select_confirmed_mapping(mappings: list[Any], inspection: Any) -> Any | None:
    sheets = {sheet.name: sheet for sheet in inspection.sheets}
    for mapping in mappings:
        sheet = sheets.get(mapping.worksheet_name)
        if sheet is None or mapping.header_row not in sheet.candidate_header_rows:
            continue
        labels = {
            label.strip().casefold()
            for label in sheet.header_labels.get(mapping.header_row, [])
            if isinstance(label, str)
        }
        expected = [
            label.strip().casefold()
            for label in mapping.column_mapping.values()
            if isinstance(label, str)
        ]
        if expected and all(label in labels for label in expected):
            return mapping
    return None


async def _process_gmail_message(
    *,
    tenant: TenantContext,
    connection_id: uuid.UUID,
    mailbox_email: str,
    history_id: str,
    message_id: str,
    message: dict[str, Any],
    gmail: GmailClient,
    access_token: str,
    force_reprocess: bool = False,
) -> None:
    sender_email, subject, snippet, received_at = _message_metadata(message)
    attachments = extract_xlsx_attachments(message)
    persisted_attachment_ids = _attachment_identities(attachments)

    async with tenant_session(tenant) as session:
        service = SupplierQuoteService(session, tenant)
        supplier_message, should_process = await service.get_or_create_message(
            connection_id=connection_id,
            gmail_message_id=message_id,
            history_id=history_id,
            sender_email=sender_email,
            received_at=received_at,
            attachment_ids=persisted_attachment_ids,
            force_reprocess=force_reprocess,
        )
        if not should_process:
            return

        suppliers = await service.list_suppliers()
        classification = await classify_supplier_email(
            sender_email=sender_email,
            subject=subject,
            snippet=snippet,
            suppliers=suppliers,
        )
        supplier_message.classification = classification.classification
        supplier_message.classification_confidence = classification.confidence
        if classification.classification == "non_supplier" and not classification.review_required:
            await service.record_processing_status(supplier_message.id, status="ignored")
            return
        if classification.review_required or classification.supplier_id is None:
            await service.record_processing_status(
                supplier_message.id,
                status="review_required",
                error_code="supplier_classification_review",
            )
            return

        supplier_id = uuid.UUID(classification.supplier_id)
        supplier_message.supplier_id = supplier_id
        if not attachments:
            await service.record_processing_status(
                supplier_message.id,
                status="review_required",
                error_code="xlsx_attachment_missing",
            )
            return

        confirmed_mappings = await service.list_confirmed_mappings(supplier_id)
        any_review = False
        affected_product_ids: set[uuid.UUID] = set()
        llm_matches_left = _LLM_MATCHES_PER_MESSAGE
        for attachment in attachments:
            unreadable = _attachment_size_issue(attachment, gmail.max_attachment_bytes)
            workbook_bytes: bytes | None = None
            if unreadable is None:
                try:
                    workbook_bytes = await fetch_attachment_if_available(
                        gmail, access_token, message_id, attachment
                    )
                except (GmailSyncError, GmailAPIError) as exc:
                    if isinstance(exc, GmailAPIError) and exc.status_code is not None:
                        raise
                    # No status code: the client itself refused the payload
                    # (over the limit once decoded, or not valid base64).
                    unreadable = _file_issue(
                        "file_too_large" if "size limit" in str(exc) else "invalid_workbook"
                    )
            if unreadable is not None:
                await service.upsert_quote_rows(
                    supplier_id,
                    supplier_message.id,
                    attachment.attachment_id,
                    [],
                    row_issues=[unreadable],
                )
                any_review = True
                continue
            if workbook_bytes is None:
                await service.upsert_quote_rows(
                    supplier_id,
                    supplier_message.id,
                    attachment.attachment_id,
                    [],
                    row_issues=[
                        WorkbookRowIssue(
                            code="gmail_attachment_unavailable",
                            message=(
                                "The Gmail attachment is no longer available; "
                                "refresh the message or request another copy."
                            ),
                        )
                    ],
                )
                any_review = True
                continue
            inspection = inspect_supplier_workbook(workbook_bytes)
            mapping = _select_confirmed_mapping(confirmed_mappings, inspection)
            if mapping is None:
                proposal_result = await suggest_mapping(inspection)
                proposal_suggestion = proposal_result.suggestion
                target_sheet = next(
                    (
                        sheet
                        for sheet in inspection.sheets
                        if proposal_suggestion is not None
                        and sheet.name == proposal_suggestion.worksheet_name
                    ),
                    inspection.sheets[0] if inspection.sheets else None,
                )
                target_header = (
                    proposal_suggestion.header_row
                    if proposal_suggestion is not None
                    else target_sheet.candidate_header_rows[0]
                    if target_sheet is not None and target_sheet.candidate_header_rows
                    else None
                )
                mapping_id = None
                if target_sheet is not None and target_header is not None:
                    labels_raw = target_sheet.header_labels.get(target_header, [])
                    labels = [
                        label.strip()[:128]
                        for label in labels_raw
                        if isinstance(label, str) and label.strip()
                    ]
                    proposal_record = await service.save_mapping_proposal(
                        supplier_id,
                        worksheet_name=target_sheet.name,
                        header_row=target_header,
                        column_mapping=(
                            {field: label for field, label in proposal_suggestion.columns.items()}
                            if proposal_suggestion is not None
                            else {}
                        ),
                        column_labels=labels or ["Unrecognized columns"],
                        source_message_id=supplier_message.id,
                        source_attachment_id=attachment.attachment_id,
                    )
                    mapping_id = proposal_record.id
                issue = WorkbookRowIssue(
                    worksheet_name=target_sheet.name if target_sheet else None,
                    code="mapping_review_required",
                    message=(
                        "A proposed column mapping needs owner confirmation."
                        if proposal_suggestion is not None
                        else (
                            "Choose the worksheet, header row, columns, "
                            "currency, and purchase unit."
                        )
                    ),
                )
                await service.upsert_quote_rows(
                    supplier_id,
                    supplier_message.id,
                    attachment.attachment_id,
                    [],
                    mapping_id=mapping_id,
                    row_issues=[*inspection.issues, issue],
                )
                any_review = True
                continue

            try:
                workbook_mapping = WorkbookMapping.model_validate(
                    {
                        "worksheet_name": mapping.worksheet_name,
                        "header_row": mapping.header_row,
                        "columns": mapping.column_mapping,
                        "default_currency": mapping.default_currency,
                        "default_purchase_unit": mapping.default_purchase_unit,
                    }
                )
            except ValueError:
                await _record_mapping_review(
                    service,
                    supplier_id=supplier_id,
                    message=supplier_message,
                    attachment=attachment,
                    inspection=inspection,
                )
                any_review = True
                continue

            parsed = parse_supplier_workbook(workbook_bytes, workbook_mapping)
            structural_issue = any(
                issue.code
                in {
                    "worksheet_not_found",
                    "header_row_not_found",
                    "column_not_found",
                    "ambiguous_column",
                }
                for issue in parsed.issues
            )
            mapping_id = mapping.id
            row_issues = list(parsed.issues)
            if structural_issue:
                proposal_result = await suggest_mapping(inspection)
                proposal_suggestion = proposal_result.suggestion
                if proposal_suggestion is not None:
                    proposed_sheet = next(
                        sheet
                        for sheet in inspection.sheets
                        if sheet.name == proposal_suggestion.worksheet_name
                    )
                    proposal_record = await service.save_mapping_proposal(
                        supplier_id,
                        worksheet_name=proposal_suggestion.worksheet_name,
                        header_row=proposal_suggestion.header_row,
                        column_mapping={
                            field: label for field, label in proposal_suggestion.columns.items()
                        },
                        column_labels=[
                            label.strip()[:128]
                            for label in proposed_sheet.header_labels.get(
                                proposal_suggestion.header_row, []
                            )
                            if isinstance(label, str) and label.strip()
                        ]
                        or ["Unrecognized columns"],
                        source_message_id=supplier_message.id,
                        source_attachment_id=attachment.attachment_id,
                    )
                    mapping_id = proposal_record.id
                row_issues.append(
                    WorkbookRowIssue(
                        worksheet_name=mapping.worksheet_name,
                        code="mapping_review_required",
                        message="The workbook layout changed; confirm a new column mapping.",
                    )
                )
                any_review = True
                parsed_rows: list[CanonicalQuoteRow] = []
            else:
                parsed_rows = parsed.rows
                any_review = any_review or bool(parsed.issues)

            products = await _load_product_candidates(session, tenant, parsed_rows)
            match_results: dict[tuple[str, int], ProductMatchResult] = {}
            product_by_id = {str(product.id): product for product in products}
            index = _ProductIndex(products)
            for row in parsed_rows:
                match = deterministic_product_match(row, index.lookup(row))
                if match is None and llm_matches_left > 0:
                    llm_matches_left -= 1
                    # difflib over the catalogue is CPU-bound; keep it off the
                    # event loop the worker's other jobs share.
                    candidates = await asyncio.to_thread(_candidate_products, row, products)
                    match = await suggest_product_match(row, candidates)
                if match is None:
                    match = ProductMatchResult(
                        status="unmatched",
                        confidence=0,
                        reason="product_match_not_attempted",
                        review_required=True,
                    )
                match_results[(row.worksheet_name, row.source_row_number)] = match
                if match.review_required:
                    row_issues.append(
                        WorkbookRowIssue(
                            worksheet_name=row.worksheet_name,
                            source_row_number=row.source_row_number,
                            code="product_match_review",
                            message="Confirm the suggested catalogue product match.",
                        )
                    )
                    any_review = True

            upsert = await service.upsert_quote_rows(
                supplier_id,
                supplier_message.id,
                attachment.attachment_id,
                parsed_rows,
                mapping_id=mapping_id,
                row_issues=row_issues,
            )
            for quote in upsert.quotes:
                stored_match = match_results.get((quote.worksheet_name, quote.source_row_number))
                if stored_match is None:
                    continue
                quote.match_status = stored_match.status
                quote.match_confidence = stored_match.confidence
                quote.product_id = (
                    uuid.UUID(stored_match.product_id) if stored_match.product_id else None
                )
                product = product_by_id.get(stored_match.product_id or "")
                if product is not None:
                    quote.catalog_sale_price = product.price
                    quote.catalog_sale_currency = product.currency
                    quote.catalog_sale_unit = _product_sale_unit(product)
                    quote.catalog_sale_price_updated_at = product.price_updated_at
                if stored_match.status == "confirmed" and quote.product_id is not None:
                    affected_product_ids.add(quote.product_id)

        if affected_product_ids:
            await _recalculate_product_comparisons(
                session,
                tenant,
                supplier_message.id,
                affected_product_ids,
            )
        await service.record_processing_status(
            supplier_message.id,
            status="review_required" if any_review else "processed",
            error_code="items_need_review" if any_review else None,
        )


async def _record_mapping_review(
    service: SupplierQuoteService,
    *,
    supplier_id: uuid.UUID,
    message: SupplierMessage,
    attachment: GmailAttachment,
    inspection: Any,
) -> None:
    sheet = inspection.sheets[0] if inspection.sheets else None
    header_row = sheet.candidate_header_rows[0] if sheet and sheet.candidate_header_rows else None
    mapping_id = None
    issues = list(inspection.issues)
    if sheet is not None and header_row is not None:
        labels = [
            label.strip()[:128]
            for label in sheet.header_labels.get(header_row, [])
            if isinstance(label, str) and label.strip()
        ]
        proposal = await service.save_mapping_proposal(
            supplier_id,
            worksheet_name=sheet.name,
            header_row=header_row,
            column_mapping={},
            column_labels=labels or ["Unrecognized columns"],
            source_message_id=message.id,
            source_attachment_id=attachment.attachment_id,
        )
        mapping_id = proposal.id
    issues.append(
        WorkbookRowIssue(
            worksheet_name=sheet.name if sheet else None,
            code="mapping_review_required",
            message="The saved mapping is invalid for this file; confirm a replacement mapping.",
        )
    )
    await service.upsert_quote_rows(
        supplier_id,
        message.id,
        attachment.attachment_id,
        [],
        mapping_id=mapping_id,
        row_issues=issues,
    )


def _message_metadata(
    message: dict[str, Any],
) -> tuple[str, str, str, datetime | None]:
    payload = message.get("payload")
    headers = payload.get("headers", []) if isinstance(payload, dict) else []
    values: dict[str, str] = {}
    if isinstance(headers, list):
        for header in headers:
            if isinstance(header, dict):
                name, value = header.get("name"), header.get("value")
                if isinstance(name, str) and isinstance(value, str):
                    values.setdefault(name.casefold(), value)
    sender = parseaddr(values.get("from", ""))[1].strip().casefold()
    subject = values.get("subject", "")[:500]
    snippet = message.get("snippet", "")
    snippet_text = snippet[:_MAX_PROMPT_TEXT] if isinstance(snippet, str) else ""
    raw_received = message.get("internalDate")
    if isinstance(raw_received, (str, int)) and not isinstance(raw_received, bool):
        try:
            received_at = datetime.fromtimestamp(int(raw_received) / 1000, tz=UTC)
        except (ValueError, OSError, OverflowError):
            received_at = None
    else:
        received_at = None
    return sender, subject, snippet_text, received_at


async def _load_product_candidates(
    session: AsyncSession,
    tenant: TenantContext,
    rows: list[CanonicalQuoteRow],
) -> list[Product]:
    if not rows:
        return []
    result = await session.execute(
        select(Product)
        .where(
            Product.organization_id == tenant.organization_id,
            Product.is_active.is_(True),
        )
        .order_by(Product.external_id, Product.id)
        .limit(5000)
    )
    return list(result.scalars().all())


def _attachment_identities(attachments: list[GmailAttachment]) -> list[dict[str, Any]]:
    return [
        {"attachment_id": attachment.attachment_id, "mime_type": attachment.mime_type}
        for attachment in attachments
    ]


_FILE_ISSUE_MESSAGES = {
    "file_too_large": "The attachment exceeds the configured size limit.",
    "invalid_workbook": "The attachment is not a readable XLSX workbook.",
}


def _file_issue(code: str) -> WorkbookRowIssue:
    return WorkbookRowIssue(code=code, message=_FILE_ISSUE_MESSAGES[code])


def _attachment_size_issue(attachment: GmailAttachment, max_bytes: int) -> WorkbookRowIssue | None:
    """An attachment Gmail already reports as too large is never downloaded."""
    if attachment.size is not None and attachment.size > max_bytes:
        return _file_issue("file_too_large")
    return None


class _ProductIndex:
    """Catalogue lookups by SKU and normalized name, built once per message.

    `deterministic_product_match` scans what it is given; handing it only the
    products that share a row's SKU or name keeps its answer identical (every
    duplicate is still in the bucket) without a whole-catalogue scan per row.
    """

    def __init__(self, products: list[Product]) -> None:
        self._by_sku: dict[str, list[Product]] = {}
        self._by_name: dict[str, list[Product]] = {}
        for product in products:
            self._by_sku.setdefault(product.external_id, []).append(product)
            name = normalize_product_name(product.name)
            if name:
                self._by_name.setdefault(name, []).append(product)

    def lookup(self, row: CanonicalQuoteRow) -> list[Product]:
        found: dict[uuid.UUID, Product] = {}
        sku = (row.supplier_sku or "").strip()
        for product in self._by_sku.get(sku, []) if sku else []:
            found[product.id] = product
        name = normalize_product_name(row.product_name)
        for product in self._by_name.get(name, []) if name else []:
            found[product.id] = product
        return list(found.values())


def _candidate_products(row: CanonicalQuoteRow, products: list[Product]) -> list[Product]:
    if not products:
        return []
    source = (row.product_name or row.supplier_sku or "").casefold().strip()
    scored: list[tuple[float, str, Product]] = []
    for product in products:
        sku_score = difflib.SequenceMatcher(
            a=(row.supplier_sku or "").casefold(),
            b=product.external_id.casefold(),
        ).ratio()
        name_score = difflib.SequenceMatcher(
            a=source,
            b=product.name.casefold(),
        ).ratio()
        scored.append((max(sku_score, name_score), product.external_id, product))
    return [
        product
        for score, _sku, product in sorted(scored, key=lambda item: (-item[0], item[1]))
        if score > 0
    ][:_PRODUCT_MATCH_CANDIDATES]


def _product_sale_unit(product: Product) -> str | None:
    attributes = product.attributes
    if not isinstance(attributes, dict):
        return None
    value = attributes.get("sale_unit") or attributes.get("unit")
    return value.strip() if isinstance(value, str) and value.strip() else None


async def _recalculate_product_comparisons(
    session: AsyncSession,
    tenant: TenantContext,
    message_id: uuid.UUID,
    product_ids: set[uuid.UUID],
) -> None:
    today = datetime.now(UTC).date()
    quote_result = await session.execute(
        select(SupplierQuote, Supplier)
        .join(
            Supplier,
            (Supplier.id == SupplierQuote.supplier_id)
            & (Supplier.organization_id == SupplierQuote.organization_id),
        )
        .where(
            SupplierQuote.organization_id == tenant.organization_id,
            SupplierQuote.product_id.in_(product_ids),
            SupplierQuote.match_status == "confirmed",
            Supplier.is_active.is_(True),
            or_(SupplierQuote.valid_until.is_(None), SupplierQuote.valid_until >= today),
        )
    )
    quote_rows = list(quote_result.all())
    products_result = await session.execute(
        select(Product).where(
            Product.organization_id == tenant.organization_id,
            Product.id.in_(product_ids),
        )
    )
    products_by_id = {product.id: product for product in products_result.scalars().all()}
    rows_by_id = {str(quote.id): quote for quote, _supplier in quote_rows}
    offers = [
        SupplierOffer(
            product_id=str(quote.product_id),
            quote_id=str(quote.id),
            supplier_id=str(supplier.id),
            supplier_name=supplier.name,
            unit_price=quote.unit_price,
            currency=quote.currency,
            purchase_unit=quote.purchase_unit,
            quantity_tiers=quote.quantity_tiers,
        )
        for quote, supplier in quote_rows
        if quote.product_id is not None
    ]
    comparisons: list[dict[str, Any]] = []
    offers_by_product = group_offers_by_product(offers)
    for product_id, product_offers in offers_by_product.items():
        product = products_by_id.get(uuid.UUID(product_id))
        if product is None:
            continue
        comparison = compare_product_offers(product_offers, product, datetime.now(UTC))
        groups: list[dict[str, Any]] = []
        for group in comparison.offer_groups:
            for ranked in group.offers:
                quote = rows_by_id.get(ranked.quote_id or "")
                if quote is None:
                    continue
                quote.catalog_sale_price = product.price
                quote.catalog_sale_currency = product.currency
                quote.catalog_sale_unit = _product_sale_unit(product)
                quote.catalog_sale_price_updated_at = product.price_updated_at
                quote.profit_status = ranked.profit_status
                quote.unit_gross_profit = ranked.unit_gross_profit
                quote.margin_percent = ranked.margin
                quote.purchase_price_rank = ranked.rank
                quote.ranking_basis = group.ranking_basis
                quote.comparability_reasons = ranked.comparability_reasons
            groups.append(group.model_dump(mode="json"))
        comparisons.append(
            {
                "product_id": product_id,
                "product_name": product.name,
                "profit_status": comparison.profit_status,
                "ranking_basis": comparison.ranking_basis,
                "sale_price_source_timestamp": (
                    comparison.sale_price_source_timestamp.isoformat()
                    if comparison.sale_price_source_timestamp
                    else None
                ),
                "offer_groups": groups,
            }
        )

    report_result = await session.execute(
        select(SupplierReport).where(
            SupplierReport.organization_id == tenant.organization_id,
            SupplierReport.supplier_message_id == message_id,
        )
    )
    report = report_result.scalar_one_or_none()
    if report is None:
        return
    summary = dict(report.summary_json)
    summary["product_comparisons"] = comparisons
    report.summary_json = summary
    known_profits = [
        quote.unit_gross_profit
        for quote, _supplier in quote_rows
        if quote.unit_gross_profit is not None
    ]
    known_margins = [
        quote.margin_percent for quote, _supplier in quote_rows if quote.margin_percent is not None
    ]
    report.best_profit = max(known_profits) if known_profits else None
    report.best_margin_percent = max(known_margins) if known_margins else None
    report.profit_status = (
        "available"
        if known_profits
        else comparisons[0]["profit_status"]
        if comparisons
        else "review_required"
    )

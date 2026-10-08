from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.dependencies import get_current_tenant
from app.core.config import get_settings
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.redis import get_redis
from app.core.tenancy import TenantContext, tenant_session, untenanted_session
from app.db.models import GmailConnection, Membership, MembershipRole, User
from app.db.models.supplier import SupplierMapping
from app.db.models.supplier_quote import SupplierMessage
from app.gmail.client import GmailClient
from app.gmail.oauth import (
    GmailOAuthClient,
    OAuthStateError,
    OAuthStateStore,
    build_authorization_url,
)
from app.gmail.token_crypto import decrypt_token, encrypt_token
from app.supplier_quotes.schemas import WorkbookMapping
from app.supplier_quotes.service import SupplierQuoteService

router = APIRouter(prefix="/api/v1/gmail", tags=["gmail"])


class GmailConnectionStatus(BaseModel):
    connected: bool
    email: str | None = None
    status: str | None = None
    watch_expires_at: datetime | None = None


def _require_owner(tenant: TenantContext) -> None:
    if tenant.role is not MembershipRole.OWNER:
        raise PermissionDeniedError("only the organization owner can manage Gmail")


async def enqueue_supplier_message_reprocess(
    organization_id: str,
    supplier_message_id: str,
    mapping_id: str,
) -> None:
    from app.workers.tasks import reprocess_supplier_message_task

    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        await pool.enqueue_job(
            reprocess_supplier_message_task.__name__,
            organization_id=organization_id,
            supplier_message_id=supplier_message_id,
            mapping_id=mapping_id,
            # No `_job_id`: arq keeps a finished job's result for an hour, and
            # a fixed id would make every "queue it again" inside that hour a
            # silent no-op. A duplicate run is harmless; the reprocess is
            # idempotent per message and attachment.
        )
    finally:
        await pool.aclose()


def _oauth_state_store() -> OAuthStateStore:
    settings = get_settings()
    return OAuthStateStore(get_redis(), ttl_seconds=settings.gmail_oauth_state_ttl_seconds)


def _web_redirect(gmail_status: str, reason: str | None = None) -> RedirectResponse:
    target = get_settings().gmail_web_redirect_uri
    if not target:
        raise ValidationError("Gmail dashboard redirect URL is not configured")
    query = {"gmail": gmail_status}
    if reason:
        query["reason"] = reason
    separator = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{separator}{urlencode(query)}", status_code=303)


def _require_oauth_settings() -> tuple[str, str]:
    settings = get_settings()
    if not settings.gmail_oauth_client_id or not settings.gmail_oauth_client_secret:
        raise ValidationError("Gmail OAuth is not configured")
    if not settings.gmail_oauth_redirect_uri:
        raise ValidationError("Gmail OAuth callback URL is not configured")
    if not settings.gmail_pubsub_topic:
        raise ValidationError("Gmail Pub/Sub topic is not configured")
    if not settings.gmail_web_redirect_uri:
        raise ValidationError("Gmail dashboard redirect URL is not configured")
    try:
        encrypt_token("gmail-configuration-check")
    except (RuntimeError, ValueError) as exc:
        raise ValidationError("Gmail token encryption is not configured") from exc
    return settings.gmail_oauth_client_id, settings.gmail_oauth_redirect_uri


@router.get("/connection", response_model=GmailConnectionStatus)
async def gmail_connection_status(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> GmailConnectionStatus:
    _require_owner(tenant)
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(GmailConnection.organization_id == tenant.organization_id)
        )
        connection = result.scalar_one_or_none()
        if connection is None:
            return GmailConnectionStatus(connected=False)
        return GmailConnectionStatus(
            connected=True,
            email=connection.email,
            status=connection.status,
            watch_expires_at=connection.watch_expires_at,
        )


class ConfirmedMapping(BaseModel):
    mapping_id: uuid.UUID
    queued: bool


@router.post("/supplier-mappings/{mapping_id}/confirm", response_model=ConfirmedMapping)
async def confirm_supplier_mapping(
    mapping_id: uuid.UUID,
    mapping: WorkbookMapping,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> ConfirmedMapping:
    """Confirm a proposed workbook layout, then queue its email's re-fetch.

    REST rather than a GraphQL mutation for the ordering alone: GraphQL's
    transaction commits after the response is sent (`build_context` is a
    request-scoped dependency), so a worker queued from a resolver -- or by
    the browser right after one -- could read the proposal unconfirmed. Here
    the confirmation commits when its block ends, and only then is the job
    queued: the `app/api/evaluations.py` pattern.
    """
    _require_owner(tenant)
    async with tenant_session(tenant) as session:
        confirmed = await SupplierQuoteService(session, tenant).confirm_mapping(
            mapping_id, mapping=mapping
        )
        source_message_id = confirmed.source_message_id
    queued = False
    if source_message_id is not None:
        try:
            await enqueue_supplier_message_reprocess(
                str(tenant.organization_id), str(source_message_id), str(mapping_id)
            )
            queued = True
        except Exception:
            # The confirmation stands; the dashboard offers to queue it again.
            queued = False
    return ConfirmedMapping(mapping_id=mapping_id, queued=queued)


@router.post("/supplier-messages/{message_id}/reprocess", status_code=status.HTTP_202_ACCEPTED)
async def reprocess_supplier_message(
    message_id: uuid.UUID,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> dict[str, str]:
    """Queue a Gmail re-fetch for a message whose mapping is already confirmed."""
    _require_owner(tenant)
    async with tenant_session(tenant) as session:
        message_result = await session.execute(
            select(SupplierMessage).where(
                SupplierMessage.id == message_id,
                SupplierMessage.organization_id == tenant.organization_id,
            )
        )
        message = message_result.scalar_one_or_none()
        if message is None or message.supplier_id is None:
            raise NotFoundError("supplier message not found")
        mapping_id = await session.scalar(
            select(SupplierMapping.id)
            .where(
                SupplierMapping.organization_id == tenant.organization_id,
                SupplierMapping.supplier_id == message.supplier_id,
                SupplierMapping.source_message_id == message.id,
                SupplierMapping.is_confirmed.is_(True),
            )
            .order_by(SupplierMapping.version.desc(), SupplierMapping.id.desc())
            .limit(1)
        )
        if mapping_id is None:
            raise ValidationError("confirm a supplier workbook mapping before reprocessing")
        connection = await session.scalar(
            select(GmailConnection).where(
                GmailConnection.id == message.gmail_connection_id,
                GmailConnection.organization_id == tenant.organization_id,
                GmailConnection.status == "connected",
            )
        )
        if connection is None:
            raise ValidationError("connect Gmail before reprocessing this message")
        organization_id = str(tenant.organization_id)
        message_id_value = str(message.id)
        mapping_id_value = str(mapping_id)

    try:
        await enqueue_supplier_message_reprocess(
            organization_id,
            message_id_value,
            mapping_id_value,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="supplier message could not be queued for reprocessing",
        ) from exc
    return {"status": "queued"}


class GmailOAuthStart(BaseModel):
    authorization_url: str


@router.post("/oauth/start", response_model=GmailOAuthStart)
async def start_gmail_oauth(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> GmailOAuthStart:
    """Return Google's consent URL; the dashboard navigates there itself.

    A redirect would be unusable: this route needs the Bearer token, and a
    browser navigation cannot send one.
    """
    _require_owner(tenant)
    if tenant.user_id is None:
        raise AuthenticationError("Gmail OAuth requires an authenticated user")
    client_id, redirect_uri = _require_oauth_settings()
    ticket = await _oauth_state_store().issue(tenant.organization_id, tenant.user_id)
    authorization_url = build_authorization_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=ticket.state,
        code_verifier=ticket.code_verifier,
    )
    return GmailOAuthStart(authorization_url=authorization_url)


@router.get("/oauth/callback")
async def gmail_oauth_callback(
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    provider_error: Annotated[str | None, Query(alias="error", max_length=128)] = None,
) -> RedirectResponse:
    if not state:
        return _web_redirect("error", "invalid_state")
    try:
        context = await _oauth_state_store().consume_for_callback(state)
    except OAuthStateError:
        return _web_redirect("error", "invalid_state")
    if provider_error or not code:
        return _web_redirect("error", "authorization_denied")

    client_id, redirect_uri = _require_oauth_settings()
    async with untenanted_session() as session:
        membership_result = await session.execute(
            select(Membership, User)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.organization_id == context.organization_id,
                Membership.user_id == context.user_id,
                Membership.role == MembershipRole.OWNER,
                User.is_active.is_(True),
            )
        )
        if membership_result.first() is None:
            raise AuthenticationError("Gmail OAuth user is no longer an organization owner")

    tenant = TenantContext(
        organization_id=context.organization_id,
        user_id=context.user_id,
        role=MembershipRole.OWNER,
        request_id="gmail-oauth-callback",
    )
    oauth = GmailOAuthClient(client_id=client_id)
    try:
        tokens = await oauth.exchange_code(
            code,
            redirect_uri=redirect_uri,
            code_verifier=context.code_verifier,
        )
        profile = await GmailClient().get_profile(tokens.access_token)
    except (RuntimeError, ValueError):
        return _web_redirect("error", "provider_error")
    mailbox_email = profile.email_address.strip().casefold()

    settings = get_settings()
    if not settings.gmail_pubsub_topic:
        return _web_redirect("error", "gmail_notifications_not_configured")

    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(GmailConnection.organization_id == tenant.organization_id)
        )
        existing = result.scalar_one_or_none()
        existing_email = existing.email if existing is not None else None
        previous_ciphertext = existing.refresh_token_ciphertext if existing is not None else None
        previous_refresh_token: str | None = None
        if existing is not None and existing.email == mailbox_email:
            previous_refresh_token = decrypt_token(existing.refresh_token_ciphertext)

    refresh_token = tokens.refresh_token or previous_refresh_token
    if not refresh_token:
        return _web_redirect("error", "refresh_token_missing")

    gmail = GmailClient()
    try:
        watch = await gmail.watch(tokens.access_token, settings.gmail_pubsub_topic)
    except RuntimeError:
        return _web_redirect("error", "gmail_notifications_unavailable")

    try:
        async with tenant_session(tenant) as session:
            result = await session.execute(
                select(GmailConnection).where(
                    GmailConnection.organization_id == tenant.organization_id
                )
            )
            connection = result.scalar_one_or_none()
            if connection is None:
                connection = GmailConnection(
                    organization_id=tenant.organization_id,
                    email=mailbox_email,
                    refresh_token_ciphertext=encrypt_token(refresh_token),
                    history_id=watch.history_id,
                    watch_expires_at=watch.expiration,
                    status="connected",
                    last_error_code=None,
                )
                session.add(connection)
            else:
                connection.email = mailbox_email
                connection.refresh_token_ciphertext = encrypt_token(refresh_token)
                if existing_email != mailbox_email or not connection.history_id:
                    connection.history_id = watch.history_id
                connection.watch_expires_at = watch.expiration
                connection.status = "connected"
                connection.last_error_code = None
    except Exception:
        try:
            await gmail.stop_watch(tokens.access_token)
        except RuntimeError:
            pass
        raise
    if existing_email and existing_email != mailbox_email and previous_ciphertext:
        try:
            old_tokens = await GmailOAuthClient().refresh_access_token(
                decrypt_token(previous_ciphertext)
            )
            await gmail.stop_watch(old_tokens.access_token)
        except (RuntimeError, ValueError):
            # Local credentials are replaced; any old provider watch expires on its own.
            pass
    return _web_redirect("connected")


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_gmail(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> Response:
    _require_owner(tenant)
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(GmailConnection.organization_id == tenant.organization_id)
        )
        connection = result.scalar_one_or_none()
        if connection is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        connection_id = connection.id
        try:
            refresh_token: str | None = decrypt_token(connection.refresh_token_ciphertext)
        except (RuntimeError, ValueError):
            refresh_token = None

    if refresh_token:
        try:
            tokens = await GmailOAuthClient().refresh_access_token(refresh_token)
            await GmailClient().stop_watch(tokens.access_token)
        except RuntimeError:
            # A revoke or expired credential must not prevent local credential deletion.
            pass

    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(GmailConnection).where(
                GmailConnection.id == connection_id,
                GmailConnection.organization_id == tenant.organization_id,
            )
        )
        connection = result.scalar_one_or_none()
        if connection is not None:
            await session.delete(connection)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

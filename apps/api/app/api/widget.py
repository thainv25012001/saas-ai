"""The public widget API, `/api/v1/widget`
(docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §4, §4.1, §5).

The one unauthenticated, internet-facing surface that spends money. It
imports nothing from `app.auth`: a widget request never carries a dashboard
JWT, and the visitor token it does carry is refused everywhere else by its
`typ` (`app/widget/tokens.py`).

Three rules every route here keeps:

* **One refusal.** Unknown key, draft/disabled agent and a widget that is
  not enabled are all the same 404 `widget not found` (Review Focus 2) --
  `_available_or_404` is the only place that decision is made.
* **Availability on every call.** A bearer route re-resolves the widget
  inside its own `tenant_session`, so turning the widget or the agent off
  takes effect on the visitor's next request, not when their 30-day token
  expires.
* **Nothing identifying in logs.** No IP, `visitor_id` or message text; a
  public key appears only as its first 8 characters. The IP exists only
  inside rate-limit keys, which expire.
"""

import hmac
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.chat import get_chat_provider
from app.api.streaming import SSE_HEADERS, stream_body
from app.chat.service import ChatService
from app.conversations.service import ConversationService
from app.core.config import get_settings
from app.core.errors import (
    AuthenticationError,
    NotFoundError,
    RateLimitError,
    WidgetDailyCapError,
)
from app.core.logging import get_logger, request_id_var
from app.core.rate_limit import enforce_rate_limit
from app.core.request import client_ip
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Conversation, ConversationChannel, ConversationStatus, MessageRole
from app.db.models.widget import WidgetPosition
from app.llm.base import LLMProvider
from app.widget.events import project_public_event
from app.widget.service import (
    PublicWidget,
    load_available,
    looks_like_public_key,
    resolve_public_key,
)
from app.widget.tokens import VisitorClaims, create_visitor_token, decode_visitor_token

router = APIRouter(prefix="/api/v1/widget", tags=["widget"])

logger = get_logger(__name__)

# Spec §5, verbatim. Module constants so a test can shrink one with
# `monkeypatch.setattr` (the same seam `app/mcp/server.py` offers).
SESSION_MINT_LIMIT = 20
SESSION_MINT_WINDOW_SECONDS = 3600
VISITOR_MESSAGE_LIMIT = 10
IP_MESSAGE_LIMIT = 30
MESSAGE_WINDOW_SECONDS = 60
DAILY_CAP_WINDOW_SECONDS = 86_400
FRAME_POLICY_LIMIT = 120
FRAME_POLICY_WINDOW_SECONDS = 60
CONFIG_LIMIT = 120
CONFIG_WINDOW_SECONDS = 60

#: The header our web middleware authenticates itself with on frame-policy.
FRAME_SECRET_HEADER = "X-Widget-Frame-Secret"

MAX_MESSAGE_LENGTH = 2_000

#: How much of a resumed conversation the widget gets back (spec §4).
RESUME_MESSAGE_LIMIT = 50
# `ConversationService.history` counts every row, tool rows and empty
# tool-use assistant rows included, so reading exactly 50 would return fewer
# than 50 user/assistant messages for a tool-heavy conversation. A tool turn
# writes at most a handful of extra rows, so four times the window is ample
# while still bounding the read.
_RESUME_SCAN_LIMIT = RESUME_MESSAGE_LIMIT * 4

_NOT_FOUND = "widget not found"
_KEY_LOG_PREFIX = 8


class WidgetConfigOut(BaseModel):
    agent_name: str
    title: str | None
    greeting: str | None
    fallback_message: str  # public copy the owner wrote for visitors; shown on a failed turn
    brand_color: str
    position: WidgetPosition


class WidgetSessionOut(BaseModel):
    token: str
    expires_at: datetime
    config: WidgetConfigOut


class WidgetMessageOut(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class WidgetConversationOut(BaseModel):
    conversation_id: uuid.UUID
    messages: list[WidgetMessageOut]


class WidgetChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    conversation_id: uuid.UUID | None = None


class FramePolicyOut(BaseModel):
    allowed_origins: list[str]


class LauncherConfigOut(BaseModel):
    """What the loader script needs before it draws anything (spec §4):
    whether to draw at all, and in which colour, on which side. All nulls
    when unavailable -- the same answer for every kind of unavailable."""

    available: bool
    brand_color: str | None
    position: WidgetPosition | None
    title: str | None


_UNAVAILABLE_LAUNCHER = LauncherConfigOut(
    available=False, brand_color=None, position=None, title=None
)


def _tenant(organization_id: uuid.UUID) -> TenantContext:
    """A widget request acts for the organization and nobody in it: no user,
    no role. Every owner/admin check in the services therefore fails closed."""
    return TenantContext(
        organization_id=organization_id,
        user_id=None,
        role=None,
        request_id=request_id_var.get(),
    )


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def visitor_from_bearer(request: Request) -> VisitorClaims:
    token = _bearer_token(request)
    if token is None:
        raise AuthenticationError("missing bearer token")
    return decode_visitor_token(token)


async def _available_or_404(
    organization_id: uuid.UUID, agent_id: uuid.UUID, session: AsyncSession
) -> PublicWidget:
    widget = await load_available(session, organization_id, agent_id)
    if widget is None:
        raise NotFoundError(_NOT_FOUND)
    return widget


def _config(widget: PublicWidget) -> WidgetConfigOut:
    return WidgetConfigOut(
        agent_name=widget.agent_name,
        title=widget.settings.title,
        greeting=widget.greeting,
        fallback_message=widget.fallback_message,
        brand_color=widget.settings.brand_color,
        position=widget.settings.position,
    )


def _reject_session(public_key: str, reason: str) -> NotFoundError:
    logger.info(
        "widget_session_rejected",
        reason=reason,
        public_key_prefix=public_key[:_KEY_LOG_PREFIX],
    )
    return NotFoundError(_NOT_FOUND)


def _reusable_visitor_id(request: Request, widget: PublicWidget) -> str | None:
    """The `vid` of an optional bearer that is a valid widget token for this
    very agent -- a returning visitor keeps their identity (and so their
    conversation). Anything else, including a garbage or expired bearer, just
    means a new visitor; it is never an error on this route."""
    token = _bearer_token(request)
    if token is None:
        return None
    try:
        claims = decode_visitor_token(token)
    except AuthenticationError:
        return None
    if claims.agent_id != widget.agent_id or claims.organization_id != widget.organization_id:
        return None
    return claims.visitor_id


def _is_trusted_frame_caller(request: Request) -> bool:
    """Our own web middleware, proven by the shared secret. Only when a
    secret is configured *and* the header matches it (constant-time)."""
    secret = get_settings().widget_frame_policy_secret
    supplied = request.headers.get(FRAME_SECRET_HEADER)
    if not secret or supplied is None:
        return False
    return hmac.compare_digest(supplied.encode(), secret.encode())


async def _available_by_key(public_key: str) -> PublicWidget | None:
    resolved = await resolve_public_key(public_key)
    if resolved is None:
        return None
    organization_id, agent_id = resolved
    async with tenant_session(_tenant(organization_id)) as session:
        return await load_available(session, organization_id, agent_id)


@router.get("/{public_key}/frame-policy")
async def frame_policy(public_key: str, request: Request) -> FramePolicyOut:
    """Always 200: `[]` for anything unavailable, so this reveals no more than
    the embed page itself would.

    A malformed key is answered before anything else, so garbage never
    touches Redis or the database. Our web middleware, proven by
    `WIDGET_FRAME_POLICY_SECRET`, is never limited: the key is public, and
    anyone could otherwise keep its budget spent and have cold middleware
    instances serve `frame-ancestors 'self'`. Everyone else is limited per
    *key*, not per IP, so a flood only ever limits the key it names. (The
    key sits only inside the Redis key, which `enforce_rate_limit` never logs
    beyond its leading scope.)"""
    if not looks_like_public_key(public_key):
        return FramePolicyOut(allowed_origins=[])
    if not _is_trusted_frame_caller(request):
        await enforce_rate_limit(
            f"widget:frame:key:{public_key}",
            limit=FRAME_POLICY_LIMIT,
            window_seconds=FRAME_POLICY_WINDOW_SECONDS,
        )
    widget = await _available_by_key(public_key)
    return FramePolicyOut(allowed_origins=widget.settings.allowed_origins if widget else [])


@router.get("/{public_key}/config")
async def launcher_config(public_key: str, response: Response) -> LauncherConfigOut:
    """The loader's pre-draw check, called from any customer's page.

    Always 200 with all nulls for anything unavailable. Readable from any
    origin (`Access-Control-Allow-Origin: *`, never credentials: nothing here
    is private) and cacheable for a minute, so an owner's change -- or the
    off switch -- reaches pages within about that long."""
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["Access-Control-Allow-Origin"] = "*"
    if not looks_like_public_key(public_key):
        return _UNAVAILABLE_LAUNCHER
    await enforce_rate_limit(
        f"widget:config:key:{public_key}",
        limit=CONFIG_LIMIT,
        window_seconds=CONFIG_WINDOW_SECONDS,
    )
    widget = await _available_by_key(public_key)
    if widget is None:
        return _UNAVAILABLE_LAUNCHER
    return LauncherConfigOut(
        available=True,
        brand_color=widget.settings.brand_color,
        position=widget.settings.position,
        title=widget.settings.title,
    )


@router.post("/{public_key}/session")
async def create_session(public_key: str, request: Request) -> WidgetSessionOut:
    resolved = await resolve_public_key(public_key)
    if resolved is None:
        raise _reject_session(public_key, "unknown_key")
    organization_id, agent_id = resolved
    async with tenant_session(_tenant(organization_id)) as session:
        widget = await load_available(session, organization_id, agent_id)
    if widget is None:
        raise _reject_session(public_key, "unavailable")

    visitor_id = _reusable_visitor_id(request, widget)
    if visitor_id is None:
        # Counted only when a new identity is minted: refreshing an existing
        # token is what every returning visitor does on page load.
        await enforce_rate_limit(
            f"widget:session:ip:{client_ip(request)}",
            limit=SESSION_MINT_LIMIT,
            window_seconds=SESSION_MINT_WINDOW_SECONDS,
        )
        visitor_id = str(uuid.uuid4())

    token, expires_at = create_visitor_token(
        organization_id=organization_id, agent_id=agent_id, visitor_id=visitor_id
    )
    return WidgetSessionOut(token=token, expires_at=expires_at, config=_config(widget))


@router.get("/conversation")
async def get_conversation(
    claims: Annotated[VisitorClaims, Depends(visitor_from_bearer)],
) -> WidgetConversationOut | None:
    """The visitor's most recent open widget conversation with this agent,
    text only, or `null`."""
    tenant = _tenant(claims.organization_id)
    async with tenant_session(tenant) as session:
        await _available_or_404(claims.organization_id, claims.agent_id, session)
        result = await session.execute(
            select(Conversation.id)
            .where(
                Conversation.organization_id == claims.organization_id,
                Conversation.agent_id == claims.agent_id,
                Conversation.visitor_id == claims.visitor_id,
                Conversation.channel == ConversationChannel.WIDGET,
                Conversation.status == ConversationStatus.OPEN,
            )
            .order_by(func.coalesce(Conversation.last_message_at, Conversation.created_at).desc())
            .limit(1)
        )
        conversation_id = result.scalar_one_or_none()
        if conversation_id is None:
            return None
        history = await ConversationService(session, tenant).history(
            conversation_id, limit=_RESUME_SCAN_LIMIT
        )

    messages = [
        WidgetMessageOut(role="user" if m.role is MessageRole.USER else "assistant", text=m.content)
        for m in history
        if m.role in (MessageRole.USER, MessageRole.ASSISTANT) and m.content
    ]
    return WidgetConversationOut(
        conversation_id=conversation_id, messages=messages[-RESUME_MESSAGE_LIMIT:]
    )


async def _enforce_daily_cap(widget: PublicWidget) -> None:
    day = datetime.now(UTC).strftime("%Y%m%d")
    try:
        await enforce_rate_limit(
            f"widget:msg:agent-day:{widget.agent_id}:{day}",
            limit=widget.settings.daily_message_cap,
            window_seconds=DAILY_CAP_WINDOW_SECONDS,
        )
    except RateLimitError as exc:
        raise WidgetDailyCapError("this assistant is not available right now") from exc


@router.post("/chat/stream")
async def widget_chat_stream(
    payload: WidgetChatRequest,
    request: Request,
    claims: Annotated[VisitorClaims, Depends(visitor_from_bearer)],
    provider: Annotated[LLMProvider | None, Depends(get_chat_provider)],
) -> StreamingResponse:
    # Per-visitor and per-IP first: both are pure Redis, so a flood is turned
    # away before it costs a database connection.
    await enforce_rate_limit(
        f"widget:msg:visitor:{claims.visitor_id}",
        limit=VISITOR_MESSAGE_LIMIT,
        window_seconds=MESSAGE_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"widget:msg:ip:{client_ip(request)}",
        limit=IP_MESSAGE_LIMIT,
        window_seconds=MESSAGE_WINDOW_SECONDS,
    )

    # Opened manually, exactly as `app/api/chat.py::chat_stream` does: the
    # session must outlive this coroutine for as long as the SSE body
    # streams, and `stream_body`'s `finally` closes it.
    tenant = _tenant(claims.organization_id)
    session_cm = tenant_session(tenant)
    session = await session_cm.__aenter__()
    try:
        widget = await _available_or_404(claims.organization_id, claims.agent_id, session)
        await _enforce_daily_cap(widget)
        events = ChatService(session, tenant, provider_override=provider).send(
            claims.agent_id,
            payload.message,
            conversation_id=payload.conversation_id,
            channel=ConversationChannel.WIDGET,
            visitor_id=claims.visitor_id,
        )
        # The first-event pre-read: another visitor's (or another agent's)
        # conversation id, an unresolvable provider -- every pre-stream
        # refusal raises here, before a `StreamingResponse` commits a 200, so
        # it is a plain JSON error (Review Focus 1's 404).
        first_event = await events.__anext__()
    except BaseException as exc:
        await session_cm.__aexit__(type(exc), exc, exc.__traceback__)
        raise

    return StreamingResponse(
        # Widget conversations are not titled (`TITLED_CHANNELS` stays
        # playground-only), and every event goes through the public
        # projection (spec §4.1).
        stream_body(
            events,
            first_event,
            session_cm,
            None,
            claims.organization_id,
            payload_fn=project_public_event,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )

"""Bearer-key authentication for `/mcp` (docs/PHASE-7.md §4, §7).

Starlette's `AuthenticationMiddleware` runs the SDK's `BearerAuthBackend`,
which hands the bearer value to `ApiKeyTokenVerifier.verify_token`. A
missing, malformed, unknown or revoked key, or one whose agent is
`disabled`, leaves the request unauthenticated, and `RequireApiKey` answers
it with **HTTP 401** before the MCP transport reads any JSON-RPC.

Nothing here caches: every request resolves its key and reads its agent's
status afresh, so a revoke or a disable takes effect on the very next
request (Review Focus 1).

The plaintext token is never logged, never put in an error, and never kept
on the `AccessToken` (its `token` field carries the key's id instead): the
only token-derived value that reaches a log line is `display_prefix`, the
same prefix the dashboard shows.
"""

import uuid

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api_keys.service import ApiKeyService, ResolvedApiKey, resolve_api_key
from app.api_keys.tokens import TOKEN_PREFIX, display_prefix, looks_like_token
from app.core.logging import get_logger, request_id_var
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import Agent, AgentStatus

logger = get_logger(__name__)

#: ASGI scope key the request id is carried under, from the request-id
#: middleware (which runs in the request's own context) to the MCP handlers
#: (which the SDK runs from its session manager's task group).
REQUEST_ID_SCOPE_KEY = "app.request_id"


class ApiKeyAccessToken(AccessToken):
    """The SDK's `AccessToken`, carrying the resolved key it was built from.

    `token` holds the key's id, not the bearer value: the SDK only needs a
    string there, and the plaintext has no business outliving the lookup.
    """

    api_key_id: uuid.UUID
    organization_id: uuid.UUID
    agent_id: uuid.UUID

    @property
    def resolved(self) -> ResolvedApiKey:
        return ResolvedApiKey(
            api_key_id=self.api_key_id,
            organization_id=self.organization_id,
            agent_id=self.agent_id,
        )


def _token_prefix(token: str) -> str | None:
    """What a rejection log line may say about the token: the dashboard's
    display prefix, and only for something shaped like one of our tokens --
    a bearer value that is not ours could be somebody else's secret."""
    return display_prefix(token) if token.startswith(TOKEN_PREFIX) else None


def _current_request_id() -> str:
    return request_id_var.get() or str(uuid.uuid4())


def _reject(reason: str, token: str | None) -> None:
    logger.warning(
        "mcp_auth_rejected",
        reason=reason,
        token_prefix=_token_prefix(token) if token is not None else None,
    )


class ApiKeyTokenVerifier(TokenVerifier):
    """`mcp.server.auth.provider.TokenVerifier` for our agent-bound keys."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not looks_like_token(token):
            _reject("malformed", token)
            return None

        resolved = await resolve_api_key(token)
        if resolved is None:
            # Unknown and revoked are one reason on purpose (spec §7): the
            # lookup cannot tell them apart and neither should a caller.
            _reject("unknown_or_revoked", token)
            return None

        tenant = TenantContext(
            organization_id=resolved.organization_id,
            user_id=None,
            role=None,
            request_id=_current_request_id(),
        )
        async with tenant_session(tenant) as session:
            # Two-layer scoped: RLS on the session, plus the explicit
            # `organization_id` predicate.
            status = await session.scalar(
                select(Agent.status).where(
                    Agent.id == resolved.agent_id,
                    Agent.organization_id == resolved.organization_id,
                )
            )
            if status is None or status == AgentStatus.DISABLED:
                # A disabled agent must not answer anyone (spec §4).
                _reject("agent_disabled", token)
                return None

            try:
                # Best-effort, and in its own savepoint: a failed
                # `last_used_at` write must neither fail this request nor
                # poison the transaction the status read ran in.
                async with session.begin_nested():
                    await ApiKeyService(session, tenant).touch_last_used(resolved.api_key_id)
            except SQLAlchemyError:
                logger.warning(
                    "mcp_touch_last_used_failed",
                    api_key_id=str(resolved.api_key_id),
                    organization_id=str(resolved.organization_id),
                )

        return ApiKeyAccessToken(
            token=str(resolved.api_key_id),
            client_id=str(resolved.api_key_id),
            scopes=[],
            api_key_id=resolved.api_key_id,
            organization_id=resolved.organization_id,
            agent_id=resolved.agent_id,
        )


class RequireApiKey:
    """401 for any `/mcp` request `AuthenticationMiddleware` did not
    authenticate -- the SDK's `RequireAuthMiddleware` without scopes, plus
    the `missing` rejection log (a request with no bearer never reaches the
    verifier, which logs every other reason itself).

    Also copies the request id onto the scope for the MCP handlers: the
    request-id middleware sets a contextvar, and the SDK runs handlers from
    its own task group, so the scope is the dependable carrier.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        user = scope.get("user")
        if not isinstance(user, AuthenticatedUser) or not isinstance(
            user.access_token, ApiKeyAccessToken
        ):
            authorization = Headers(scope=scope).get("authorization")
            if not authorization or not authorization.lower().startswith("bearer "):
                _reject("missing", None)
            await _send_401(send)
            return

        scope[REQUEST_ID_SCOPE_KEY] = _current_request_id()
        await self.app(scope, receive, send)


async def _send_401(send: Send) -> None:
    body = b'{"error":"invalid_token","error_description":"Authentication required"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (
                    b"www-authenticate",
                    b'Bearer error="invalid_token", error_description="Authentication required"',
                ),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

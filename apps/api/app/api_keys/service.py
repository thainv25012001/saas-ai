"""Agent-bound API keys (docs/PHASE-7.md §2-§3): create, list and revoke
under an ordinary tenant, plus `resolve_api_key`, the one lookup that must
run *before* an organization is known at all -- what MCP authentication
(Task 3) calls with a bearer token's hash, before any `TenantContext`
exists to build an `ApiKeyService` with.

Role check (docs/PHASE-7.md §6): creating or revoking a key requires the
organization's owner or admin -- the first role check in this product,
because a key is a credential that outlives the session of whoever made it.
`list_for_agent` needs no such check: any member may see what keys exist,
same as any member may see the agent itself.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api_keys.schemas import CreateApiKeyInput
from app.api_keys.tokens import display_prefix, generate_token, hash_token, looks_like_token
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
    format_validation_errors,
)
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, untenanted_session
from app.db.models import Agent, ApiKey, MembershipRole

MAX_ACTIVE_KEYS_PER_AGENT = 10

_PRIVILEGED_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN)


@dataclass(frozen=True, slots=True)
class CreatedApiKey:
    """`token` is the plaintext secret, readable exactly once: the caller
    that receives this value is the only place in the system it is ever
    available outside a client's own storage. `api_key` never carries it --
    only `key_hash`/`key_prefix`, per docs/PHASE-7.md §3."""

    api_key: ApiKey
    token: str


@dataclass(frozen=True, slots=True)
class ResolvedApiKey:
    api_key_id: uuid.UUID
    organization_id: uuid.UUID
    agent_id: uuid.UUID


class ApiKeyService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def list_for_agent(self, agent_id: uuid.UUID) -> list[ApiKey]:
        """Newest first, revoked keys included -- the dashboard's key list
        (docs/PHASE-7.md §6) shows a revoked key rather than hiding it, so an
        owner can see what was revoked and when.

        Cross-tenant `agent_id` (or one that does not exist at all) is a 404,
        not an empty list: unlike `LeadService.list_for_agent`, whose Query
        resolver leans on "empty list" being indistinguishable from "not
        mine" for a dashboard read, this task's brief pins cross-tenant
        `list_for_agent` to `NotFoundError` explicitly.
        """
        await self._get_agent(agent_id)
        result = await self.session.execute(
            select(ApiKey)
            .where(
                ApiKey.agent_id == agent_id,
                ApiKey.organization_id == self.tenant.organization_id,
            )
            .order_by(ApiKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def create(self, agent_id: uuid.UUID, name: str) -> CreatedApiKey:
        """Mint a new key for `agent_id`, bound to this tenant.

        Order matters: the role check comes first (a member should not learn
        anything about the agent's cap by probing it), then the agent
        ownership SELECT -- load-bearing, not belt-and-braces, the same
        reason `LeadService.create`'s own docstring gives: a Postgres FK
        check bypasses the referencing session's RLS, so an INSERT whose
        `agent_id` names another org's agent would otherwise still succeed.
        """
        self._require_privileged()
        try:
            validated_name = CreateApiKeyInput(name=name).name
        except PydanticValidationError as exc:
            raise ValidationError(format_validation_errors(exc.errors())) from exc

        await self._get_agent(agent_id)

        active_count = await self.session.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(
                ApiKey.agent_id == agent_id,
                ApiKey.organization_id == self.tenant.organization_id,
                ApiKey.revoked_at.is_(None),
            )
        )
        if (active_count or 0) >= MAX_ACTIVE_KEYS_PER_AGENT:
            raise ConflictError(f"agent already has {MAX_ACTIVE_KEYS_PER_AGENT} active API keys")

        token = generate_token()
        api_key = ApiKey(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent_id,
            name=validated_name,
            key_prefix=display_prefix(token),
            key_hash=hash_token(token),
            created_by=self.tenant.user_id,
        )
        self.session.add(api_key)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # key_hash is UNIQUE; a collision on a 256-bit random value is
            # astronomically unlikely, but a caller retrying `create` should
            # get a domain error, not a raw IntegrityError.
            raise ConflictError("could not create API key") from exc
        return CreatedApiKey(api_key=api_key, token=token)

    async def revoke(self, api_key_id: uuid.UUID) -> ApiKey:
        """Idempotent: revoking an already-revoked key is a no-op that
        returns the same row, not a second write or an error -- a dashboard
        Revoke button double-clicked, or retried after a dropped response,
        must not fail the second time."""
        self._require_privileged()
        api_key = await self._get_api_key(api_key_id)
        if api_key.revoked_at is None:
            api_key.revoked_at = datetime.now(UTC)
            await self.session.flush()
        return api_key

    async def touch_last_used(self, api_key_id: uuid.UUID) -> None:
        """At most one write per key per minute (docs/PHASE-7.md §4): an
        active MCP client calling every few seconds must not turn each call
        into an UPDATE. The WHERE clause is the throttle -- a call inside the
        window matches no row and this is a no-op, not a second read-then-
        write.

        No RLS-bearing session is required for the WHERE to be safe: the
        explicit `organization_id` predicate is Layer 1 on its own, and this
        method is always called from a tenant-scoped `ApiKeyService`."""
        await self.session.execute(
            text(
                "UPDATE api_keys SET last_used_at = now() "
                "WHERE id = :id AND organization_id = :org_id "
                "AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"
            ),
            {"id": api_key_id, "org_id": self.tenant.organization_id},
        )

    def _require_privileged(self) -> None:
        if self.tenant.role not in _PRIVILEGED_ROLES:
            raise PermissionDeniedError("owner or admin role required")

    async def _get_agent(self, agent_id: uuid.UUID) -> None:
        result = await self.session.execute(
            select(Agent.id).where(
                Agent.id == agent_id,
                Agent.organization_id == self.tenant.organization_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError("agent not found")

    async def _get_api_key(self, api_key_id: uuid.UUID) -> ApiKey:
        result = await self.session.execute(
            select(ApiKey).where(
                ApiKey.id == api_key_id,
                ApiKey.organization_id == self.tenant.organization_id,
            )
        )
        api_key = result.scalar_one_or_none()
        if api_key is None:
            raise NotFoundError("API key not found")
        return api_key


async def resolve_api_key(token: str) -> ResolvedApiKey | None:
    """The one lookup that runs before any organization is known -- what a
    bearer token authenticates to. `looks_like_token` rejects an
    obviously-wrong value before any DB call; everything past it goes
    through the `resolve_api_key` SQL function (docs/PHASE-7.md §3), a
    `SECURITY DEFINER` owned by the migrating role and therefore not itself
    subject to `api_keys`' RLS policy (see `alembic/versions/0015_api_keys.py`).
    It takes one exact hash and returns three ids, nothing else -- it cannot
    be used to enumerate keys, and a `NULL`/unknown/revoked hash yields no
    row.

    Runs in `untenanted_session()`, the same session used before
    registration/login establish an organization at all: nothing here reads
    or writes any tenant-owned table directly.
    """
    if not looks_like_token(token):
        return None
    key_hash = hash_token(token)
    async with untenanted_session() as session:
        result = await session.execute(
            text("SELECT * FROM resolve_api_key(:hash)"), {"hash": key_hash}
        )
        row = result.mappings().first()
    if row is None:
        return None
    return ResolvedApiKey(
        api_key_id=row["api_key_id"],
        organization_id=row["organization_id"],
        agent_id=row["agent_id"],
    )

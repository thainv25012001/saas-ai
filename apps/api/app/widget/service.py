"""Widget settings (dashboard side) and the public-key resolve (public side)
that Phase 8's widget is built on top of
(docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §3-§4).

Three things live here because they share one subject -- an agent's widget --
but run under three different trust levels:

* `WidgetSettingsService` is an ordinary tenant-scoped service, exactly like
  `ApiKeyService`: a dashboard caller with a real `TenantContext`, reading and
  writing under RLS plus an explicit `organization_id` predicate (Layer 1,
  docs/ARCHITECTURE.md §2.3).
* `resolve_public_key` is the one lookup that runs *before* any organization
  is known -- what a `pk_...` key found in a page's HTML resolves to. It
  mirrors `app.api_keys.service.resolve_api_key`: a regex pre-check that
  avoids a DB round trip for an obviously-wrong value, then the
  `SECURITY DEFINER` SQL function `alembic/versions/0016_widget_settings.py`
  creates, run in `untenanted_session()`.
* `load_available` runs *after* `resolve_public_key` has produced an
  organization/agent pair and the caller has opened a `tenant_session` for
  it -- so RLS is active here, and every read still repeats the
  `organization_id` predicate explicitly, the same two-layer discipline as
  everywhere else. It is the single source of truth for "is this widget
  reachable right now" (spec §4): unknown key, draft/disabled agent, and a
  disabled widget must all read identically from the outside (Review Focus
  #2), so this function -- not three separate checks scattered across a
  future router -- is what a later task calls on every widget request.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext, untenanted_session
from app.db.models import Agent, AgentConfig, AgentStatus, MembershipRole, WidgetSettings
from app.db.models.widget import WidgetPosition
from app.widget.origins import InvalidOriginError, normalize_origins
from app.widget.schemas import UpdateWidgetSettingsInput

_PRIVILEGED_ROLES = (MembershipRole.OWNER, MembershipRole.ADMIN)

_DEFAULT_BRAND_COLOR = "#2563eb"
_DEFAULT_DAILY_MESSAGE_CAP = 500

# `pk_` plus 16-60 URL-safe characters -- matches `secrets.token_urlsafe(24)`,
# the shape `AgentService.create_agent` actually mints
# (`app/agents/service.py`), with headroom on both ends rather than pinning
# the exact length that one call site happens to produce today.
_PUBLIC_KEY_RE = re.compile(r"^pk_[A-Za-z0-9_-]{16,60}$")


@dataclass(frozen=True, slots=True)
class WidgetView:
    """An agent's widget configuration, with Python-side defaults standing
    in for a row that does not exist yet -- see `WidgetSettings`'s own
    docstring for why a missing row is a normal, expected state rather than
    something to backfill."""

    agent_id: uuid.UUID
    enabled: bool
    allowed_origins: list[str]
    brand_color: str
    position: WidgetPosition
    title: str | None
    daily_message_cap: int


@dataclass(frozen=True, slots=True)
class PublicWidget:
    """An AVAILABLE widget, resolved from a public key: everything the
    public embed page (a later task) needs to render, and nothing a visitor
    should not see -- no organization name, no internal ids beyond the two
    this same request already supplied."""

    organization_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    settings: WidgetView
    greeting: str | None
    fallback_message: str


def _default_view(agent_id: uuid.UUID) -> WidgetView:
    return WidgetView(
        agent_id=agent_id,
        enabled=False,
        allowed_origins=[],
        brand_color=_DEFAULT_BRAND_COLOR,
        position=WidgetPosition.RIGHT,
        title=None,
        daily_message_cap=_DEFAULT_DAILY_MESSAGE_CAP,
    )


def _view(row: WidgetSettings) -> WidgetView:
    return WidgetView(
        agent_id=row.agent_id,
        enabled=row.enabled,
        allowed_origins=list(row.allowed_origins),
        brand_color=row.brand_color,
        position=row.position,
        title=row.title,
        daily_message_cap=row.daily_message_cap,
    )


class WidgetSettingsService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def get(self, agent_id: uuid.UUID) -> WidgetView:
        """Any member may read a widget's settings, same as any member may
        read the agent itself -- there is nothing here a member should not
        see, unlike `update`, which is owner/admin only."""
        await self._get_agent(agent_id)
        row = await self._get_row(agent_id)
        return _view(row) if row is not None else _default_view(agent_id)

    async def update(self, agent_id: uuid.UUID, data: UpdateWidgetSettingsInput) -> WidgetView:
        """Upsert this agent's widget settings.

        Order matters, same reasoning as `ApiKeyService.create`: the role
        check comes first (a member should not learn anything about the
        agent by probing it), then the agent-ownership SELECT -- load-bearing,
        not belt-and-braces, per `docs/ARCHITECTURE.md` §2.3: a Postgres FK
        check on `agent_id` bypasses this session's RLS, so the INSERT below
        would otherwise happily attach a settings row to another org's agent.
        """
        self._require_privileged()
        await self._get_agent(agent_id)

        try:
            origins = normalize_origins(
                data.allowed_origins,
                allow_localhost_http=get_settings().environment == "local",
            )
        except InvalidOriginError as exc:
            raise ValidationError(str(exc)) from exc

        statement = pg_insert(WidgetSettings).values(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent_id,
            enabled=data.enabled,
            allowed_origins=origins,
            brand_color=data.brand_color,
            position=data.position,
            title=data.title,
            daily_message_cap=data.daily_message_cap,
        )
        upsert_statement = statement.on_conflict_do_update(
            constraint="uq_widget_settings_agent_id",
            set_={
                "enabled": statement.excluded.enabled,
                "allowed_origins": statement.excluded.allowed_origins,
                "brand_color": statement.excluded.brand_color,
                "position": statement.excluded.position,
                "title": statement.excluded.title,
                "daily_message_cap": statement.excluded.daily_message_cap,
                # A Core upsert skips the ORM's `onupdate`, so set it here.
                "updated_at": func.now(),
            },
        ).returning(WidgetSettings.id)
        row_id = (await self.session.execute(upsert_statement)).scalar_one()
        await self.session.flush()

        # Re-select through the ORM rather than trust the row this session's
        # identity map already holds (same reasoning as
        # `ProductService.upsert_many`'s own re-select): `populate_existing`
        # makes sure a settings object already loaded earlier in this
        # transaction (e.g. by `get`) is refreshed with what the UPDATE just
        # wrote, not handed back stale.
        refreshed = await self.session.execute(
            select(WidgetSettings)
            .where(
                WidgetSettings.id == row_id,
                WidgetSettings.organization_id == self.tenant.organization_id,
            )
            .execution_options(populate_existing=True)
        )
        return _view(refreshed.scalar_one())

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

    async def _get_row(self, agent_id: uuid.UUID) -> WidgetSettings | None:
        result = await self.session.execute(
            select(WidgetSettings).where(
                WidgetSettings.agent_id == agent_id,
                WidgetSettings.organization_id == self.tenant.organization_id,
            )
        )
        return result.scalar_one_or_none()


def looks_like_public_key(public_key: str) -> bool:
    """The shape check alone, for a route that must turn a garbage key away
    before it spends a rate-limit counter or a database round trip."""
    return _PUBLIC_KEY_RE.match(public_key) is not None


async def resolve_public_key(public_key: str) -> tuple[uuid.UUID, uuid.UUID] | None:
    """The one lookup that runs before any organization is known -- what a
    widget loader's `data-key` resolves to. `_PUBLIC_KEY_RE` rejects an
    obviously-wrong value before any DB call, exactly as
    `app.api_keys.tokens.looks_like_token` does for API keys; everything past
    it goes through `resolve_widget`, the `SECURITY DEFINER` SQL function
    `alembic/versions/0016_widget_settings.py` creates. It reads only
    `agents.public_key` and returns two ids, nothing else -- it cannot be
    used to enumerate agents, and an unknown key yields no row.

    Runs in `untenanted_session()`, the same session used before any
    `TenantContext` exists at all: nothing here reads or writes any
    tenant-owned table directly.
    """
    if not looks_like_public_key(public_key):
        return None
    async with untenanted_session() as session:
        result = await session.execute(
            text("SELECT * FROM resolve_widget(:key)"), {"key": public_key}
        )
        row = result.mappings().first()
    if row is None:
        return None
    return row["organization_id"], row["agent_id"]


async def load_available(
    session: AsyncSession, organization_id: uuid.UUID, agent_id: uuid.UUID
) -> PublicWidget | None:
    """Whether this agent's widget is reachable right now, and if so,
    everything a public embed page needs to render it.

    Called inside the caller's own `tenant_session(TenantContext(organization_id=
    organization_id, user_id=None, role=None, ...))` (spec §4), so RLS is
    already scoped to `organization_id` for every statement below -- but each
    read repeats that predicate explicitly anyway (two-layer tenancy,
    docs/ARCHITECTURE.md §2.3), the same discipline `WidgetSettingsService`
    applies, because the caller here is a public route with no membership
    check at all, the one place in this codebase where Layer 1 slipping would
    be most exploitable.

    `None` for every one of: unknown agent, a draft/disabled agent, a missing
    config row, a missing settings row, or a settings row with `enabled`
    false. The caller collapses all of these into the identical public 404
    (Review Focus #2) -- this function does not distinguish them even in its
    return type, on purpose.
    """
    agent_result = await session.execute(
        select(Agent).where(
            Agent.id == agent_id,
            Agent.organization_id == organization_id,
        )
    )
    agent = agent_result.scalar_one_or_none()
    if agent is None or agent.status is not AgentStatus.ACTIVE:
        return None

    config_result = await session.execute(
        select(AgentConfig).where(
            AgentConfig.agent_id == agent_id,
            AgentConfig.organization_id == organization_id,
        )
    )
    config = config_result.scalar_one_or_none()
    if config is None:
        return None

    settings_result = await session.execute(
        select(WidgetSettings).where(
            WidgetSettings.agent_id == agent_id,
            WidgetSettings.organization_id == organization_id,
        )
    )
    settings_row = settings_result.scalar_one_or_none()
    if settings_row is None or not settings_row.enabled:
        return None

    return PublicWidget(
        organization_id=organization_id,
        agent_id=agent_id,
        agent_name=agent.name,
        settings=_view(settings_row),
        greeting=config.greeting,
        fallback_message=config.fallback_message,
    )

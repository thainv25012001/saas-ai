"""Task 7 (ruling R4): the dev seed enables the demo agent's widget so
`infrastructure/widget-demo` has something to point at with no manual
dashboard step.

Integration, not unit, because `seed()` writes through `WidgetSettingsService`
under RLS -- `tests/unit/test_seed.py` covers `demo_agent_input()`, which
needs no database at all.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
from app.db.models.widget import WidgetPosition
from app.db.seed import DEMO_AGENT_SLUG, DEMO_ORG_SLUG, DEMO_WIDGET_ORIGIN, seed
from app.widget.schemas import UpdateWidgetSettingsInput
from app.widget.service import WidgetSettingsService, load_available

pytestmark = pytest.mark.anyio


async def _demo_ids(owner_connection: AsyncConnection) -> tuple[uuid.UUID, uuid.UUID]:
    """Read the seeded org/agent ids as `app_owner`, bypassing RLS -- the
    same pattern `tests/conftest.py`'s `_make_tenant` uses to set fixture
    data up. Read-committed isolation on a fresh statement is enough to see
    what `seed()` already committed on its own connection."""
    org_row = (
        (
            await owner_connection.execute(
                text("SELECT id FROM organizations WHERE slug = :slug"),
                {"slug": DEMO_ORG_SLUG},
            )
        )
        .mappings()
        .first()
    )
    assert org_row is not None, "seed() did not create the demo organization"
    org_id: uuid.UUID = org_row["id"]

    agent_row = (
        (
            await owner_connection.execute(
                text("SELECT id FROM agents WHERE organization_id = :org_id AND slug = :slug"),
                {"org_id": org_id, "slug": DEMO_AGENT_SLUG},
            )
        )
        .mappings()
        .first()
    )
    assert agent_row is not None, "seed() did not create the demo agent"
    agent_id: uuid.UUID = agent_row["id"]
    return org_id, agent_id


async def test_seed_enables_the_demo_widget_for_localhost_5500(
    owner_connection: AsyncConnection,
) -> None:
    await seed()

    org_id, agent_id = await _demo_ids(owner_connection)
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    async with tenant_session(tenant) as session:
        view = await WidgetSettingsService(session, tenant).get(agent_id)

    assert view.enabled is True
    assert view.allowed_origins == [DEMO_WIDGET_ORIGIN]


async def test_seed_activates_the_demo_agent_so_the_widget_is_publicly_available(
    owner_connection: AsyncConnection,
) -> None:
    """The real end-to-end property (Task 7 review finding): enabling
    `widget_settings` alone is not enough. `load_available`
    (`app/widget/service.py`, spec §4) treats a `draft` agent identically to
    an unknown key -- a freshly created agent starts `draft`
    (`AgentService.create_agent`) -- so without also activating it, the seed
    still leaves a widget that 404s for every visitor, defeating the point
    of `infrastructure/widget-demo`."""
    await seed()

    org_id, agent_id = await _demo_ids(owner_connection)

    status_row = (
        (
            await owner_connection.execute(
                text("SELECT status FROM agents WHERE id = :id"), {"id": agent_id}
            )
        )
        .mappings()
        .first()
    )
    assert status_row is not None
    assert status_row["status"] == "active"

    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )
    async with tenant_session(tenant) as session:
        widget = await load_available(session, org_id, agent_id)

    assert widget is not None
    assert widget.settings.enabled is True
    assert widget.settings.allowed_origins == [DEMO_WIDGET_ORIGIN]


async def test_seed_is_idempotent_and_does_not_clobber_a_manual_change(
    owner_connection: AsyncConnection,
) -> None:
    """Re-running the seed (the documented, repeatable workflow) must not
    leave a stale value behind: `seed()` always upserts to its own fixed
    input, so a second run after some other change still lands on exactly
    the seed's values, not whatever the last write happened to set."""
    await seed()
    org_id, agent_id = await _demo_ids(owner_connection)
    tenant = TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )

    async with tenant_session(tenant) as session:
        await WidgetSettingsService(session, tenant).update(
            agent_id,
            UpdateWidgetSettingsInput(
                enabled=False,
                allowed_origins=[],
                brand_color="#000000",
                position=WidgetPosition.LEFT,
                title=None,
                daily_message_cap=1,
            ),
        )

    await seed()

    async with tenant_session(tenant) as session:
        view = await WidgetSettingsService(session, tenant).get(agent_id)
    assert view.enabled is True
    assert view.allowed_origins == [DEMO_WIDGET_ORIGIN]

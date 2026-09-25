import dataclasses

import pytest
from sqlalchemy import text

from app.agents.schemas import UpdateAgentConfigInput, UpdateAgentInput
from app.agents.service import AgentService
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MembershipRole
from app.db.models.widget import WidgetPosition
from app.db.session import session_factory
from app.widget.schemas import UpdateWidgetSettingsInput
from app.widget.service import WidgetSettingsService, WidgetView, load_available, resolve_public_key
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _agent(session, tenant):
    return await AgentService(session, tenant).create_agent(agent_input("Sales Bot"))


def _member(tenant: TenantContext) -> TenantContext:
    """Same trick as `test_api_key_service.py::_member`: `TenantContext.role`
    is checked purely in Python, never by RLS, so a member-role context for
    the same organization needs no second membership row."""
    return dataclasses.replace(tenant, role=MembershipRole.MEMBER)


def _valid_input(**overrides: object) -> UpdateWidgetSettingsInput:
    fields: dict[str, object] = {
        "enabled": True,
        "allowed_origins": ["https://shop.example.com"],
        "brand_color": "#123ABC",
        "position": WidgetPosition.LEFT,
        "title": "Chat with us",
        "daily_message_cap": 250,
        **overrides,
    }
    return UpdateWidgetSettingsInput(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


async def test_get_with_no_row_returns_defaults(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        view = await WidgetSettingsService(session, tenant_a).get(agent.id)

    assert view == WidgetView(
        agent_id=agent.id,
        enabled=False,
        allowed_origins=[],
        brand_color="#2563eb",
        position=WidgetPosition.RIGHT,
        title=None,
        daily_message_cap=500,
    )


async def test_member_role_can_read_settings(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    member = _member(tenant_a)
    async with tenant_session(member) as session:
        view = await WidgetSettingsService(session, member).get(agent.id)
    assert view.enabled is False


async def test_get_for_another_tenants_agent_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await WidgetSettingsService(session, tenant_b).get(agent.id)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


async def test_update_inserts_then_updates_a_single_row(tenant_a, owner_connection):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        first = await WidgetSettingsService(session, tenant_a).update(
            agent.id, _valid_input(daily_message_cap=250)
        )
    assert first.daily_message_cap == 250
    assert first.brand_color == "#123abc"
    assert first.title == "Chat with us"
    assert first.position == WidgetPosition.LEFT

    async with tenant_session(tenant_a) as session:
        second = await WidgetSettingsService(session, tenant_a).update(
            agent.id, _valid_input(daily_message_cap=999)
        )
    assert second.daily_message_cap == 999

    count = (
        await owner_connection.execute(
            text("SELECT COUNT(*) FROM widget_settings WHERE agent_id = :id"),
            {"id": agent.id},
        )
    ).scalar_one()
    assert count == 1


async def test_update_bumps_updated_at_on_a_second_update(tenant_a, owner_connection):
    """The upsert's `ON CONFLICT DO UPDATE` bypasses the ORM's `onupdate`,
    so it has to set `updated_at` itself."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async def _updated_at():
        return (
            await owner_connection.execute(
                text("SELECT updated_at FROM widget_settings WHERE agent_id = :id"),
                {"id": agent.id},
            )
        ).scalar_one()

    async with tenant_session(tenant_a) as session:
        await WidgetSettingsService(session, tenant_a).update(agent.id, _valid_input())
    first = await _updated_at()

    async with tenant_session(tenant_a) as session:
        await WidgetSettingsService(session, tenant_a).update(
            agent.id, _valid_input(daily_message_cap=999)
        )
    second = await _updated_at()

    assert second > first


async def test_update_strips_and_normalizes_title_and_brand_color(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        view = await WidgetSettingsService(session, tenant_a).update(
            agent.id, _valid_input(title="  Chat  ", brand_color="#ABCDEF")
        )
    assert view.title == "Chat"
    assert view.brand_color == "#abcdef"


async def test_update_blank_title_stores_none(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        view = await WidgetSettingsService(session, tenant_a).update(
            agent.id, _valid_input(title="   ")
        )
    assert view.title is None


async def test_member_role_cannot_update(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    member = _member(tenant_a)
    async with tenant_session(member) as session:
        with pytest.raises(PermissionDeniedError):
            await WidgetSettingsService(session, member).update(agent.id, _valid_input())


async def test_update_for_another_tenants_agent_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await WidgetSettingsService(session, tenant_b).update(agent.id, _valid_input())


async def test_update_rejects_an_invalid_origin(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        with pytest.raises(ValidationError) as excinfo:
            await WidgetSettingsService(session, tenant_a).update(
                agent.id, _valid_input(allowed_origins=["https://a.com/path"])
            )
    assert repr("https://a.com/path") in str(excinfo.value)


async def test_update_normalizes_origins_and_dedupes(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_a) as session:
        view = await WidgetSettingsService(session, tenant_a).update(
            agent.id,
            _valid_input(
                allowed_origins=["https://Shop.Example.com/", "https://shop.example.com:443"]
            ),
        )
    assert view.allowed_origins == ["https://shop.example.com"]


# ---------------------------------------------------------------------------
# resolve_public_key
# ---------------------------------------------------------------------------


async def test_resolve_public_key_works_as_app_user_with_no_tenant_set(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    # Positive control, same shape as
    # test_api_key_service.py::test_resolve_api_key_works_as_app_user_with_no_tenant_set:
    # a plain, untenanted session sees nothing directly on `agents` (RLS, no
    # `app.current_org_id` set), so the resolve below is doing real work, not
    # merely re-reading something already visible.
    async with session_factory() as session:
        async with session.begin():
            plain_select = await session.execute(
                text("SELECT * FROM agents WHERE id = :id"), {"id": agent.id}
            )
            assert plain_select.first() is None

    resolved = await resolve_public_key(agent.public_key)
    assert resolved == (tenant_a.organization_id, agent.id)


async def test_resolve_public_key_returns_none_for_an_unknown_key(tenant_a):
    async with tenant_session(tenant_a) as session:
        await _agent(session, tenant_a)

    assert await resolve_public_key("pk_" + "x" * 24) is None


async def test_resolve_public_key_returns_none_for_a_malformed_key_without_querying(monkeypatch):
    """`x` does not match `_PUBLIC_KEY_RE` at all. Proven here not just by the
    return value but by patching `untenanted_session` to blow up the instant
    it is entered -- if the malformed-key short-circuit in
    `resolve_public_key` ever regressed to reaching the database first, this
    test would fail with the `AssertionError` below instead of quietly
    passing on the DB round trip's own (correct) empty result."""
    from app.widget import service as widget_service

    class _MustNotBeEntered:
        async def __aenter__(self) -> None:
            raise AssertionError("must not query the database for a malformed key")

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

    monkeypatch.setattr(widget_service, "untenanted_session", lambda: _MustNotBeEntered())

    assert await resolve_public_key("x") is None
    assert await resolve_public_key("") is None
    assert await resolve_public_key("pk_short") is None


# ---------------------------------------------------------------------------
# load_available
# ---------------------------------------------------------------------------


async def test_load_available_returns_none_for_a_draft_agent(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await WidgetSettingsService(session, tenant_a).update(agent.id, _valid_input(enabled=True))

    async with tenant_session(tenant_a) as session:
        result = await load_available(session, tenant_a.organization_id, agent.id)
    assert result is None


async def test_load_available_returns_none_for_a_disabled_agent(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await AgentService(session, tenant_a).update_agent(
            agent.id, UpdateAgentInput(status="disabled")
        )
        await WidgetSettingsService(session, tenant_a).update(agent.id, _valid_input(enabled=True))

    async with tenant_session(tenant_a) as session:
        result = await load_available(session, tenant_a.organization_id, agent.id)
    assert result is None


async def test_load_available_returns_none_when_no_settings_row_exists(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await AgentService(session, tenant_a).update_agent(
            agent.id, UpdateAgentInput(status="active")
        )
        # No widget_settings row at all -- defaults to disabled.

    async with tenant_session(tenant_a) as session:
        result = await load_available(session, tenant_a.organization_id, agent.id)
    assert result is None


async def test_load_available_returns_none_when_settings_row_is_disabled(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await AgentService(session, tenant_a).update_agent(
            agent.id, UpdateAgentInput(status="active")
        )
        await WidgetSettingsService(session, tenant_a).update(agent.id, _valid_input(enabled=False))

    async with tenant_session(tenant_a) as session:
        result = await load_available(session, tenant_a.organization_id, agent.id)
    assert result is None


async def test_load_available_returns_a_public_widget_for_active_and_enabled(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await AgentService(session, tenant_a).update_agent(
            agent.id, UpdateAgentInput(status="active")
        )
        await AgentService(session, tenant_a).update_config(
            agent.id,
            UpdateAgentConfigInput(greeting="Hi there!", fallback_message="Try again later."),
        )
        await WidgetSettingsService(session, tenant_a).update(
            agent.id, _valid_input(enabled=True, title="Chat")
        )

    async with tenant_session(tenant_a) as session:
        result = await load_available(session, tenant_a.organization_id, agent.id)

    assert result is not None
    assert result.organization_id == tenant_a.organization_id
    assert result.agent_id == agent.id
    assert result.agent_name == agent.name
    assert result.greeting == "Hi there!"
    assert result.fallback_message == "Try again later."
    assert result.settings.enabled is True
    assert result.settings.title == "Chat"


async def test_load_available_returns_none_for_an_unknown_agent_id(tenant_a):
    import uuid

    async with tenant_session(tenant_a) as session:
        result = await load_available(session, tenant_a.organization_id, uuid.uuid4())
    assert result is None


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------


async def test_rls_hides_another_tenants_widget_settings_row(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await WidgetSettingsService(session, tenant_a).update(agent.id, _valid_input())

    async with tenant_session(tenant_b) as session:
        rows = await session.execute(text("SELECT * FROM widget_settings"))
        assert rows.first() is None

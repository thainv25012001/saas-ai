import pytest

from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.prompts.schemas import CreatePromptInput, CreateVersionInput
from app.prompts.service import PromptService

pytestmark = pytest.mark.anyio


async def _prompt(session, tenant):
    return await PromptService(session, tenant).create_prompt(
        CreatePromptInput(
            name="Sales system prompt",
            key="sales_system",
            system_prompt="You are a helpful assistant for {{company_name}}.",
        )
    )


async def test_creating_a_prompt_creates_version_one(tenant_a):
    async with tenant_session(tenant_a) as session:
        prompt = await _prompt(session, tenant_a)
        version = await PromptService(session, tenant_a).active_version(prompt.id)
    assert version.version == 1
    assert version.is_active is True


async def test_new_versions_increment(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        second = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))
    assert second.version == 2


async def test_a_new_version_is_inactive_until_activated(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        second = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))
        assert second.is_active is False
        active = await service.active_version(prompt.id)
    assert active.version == 1


async def test_activating_a_version_deactivates_the_previous_one(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        second = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))
        await service.activate_version(second.id)
        active = await service.active_version(prompt.id)
    assert active.version == 2


async def test_exactly_one_version_is_active_after_repeated_switching(tenant_a):
    """The partial unique index makes 'exactly one active' a database
    guarantee. This test is what proves the index is actually there."""
    from sqlalchemy import func, select

    from app.db.models import PromptVersion

    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        v2 = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))
        v3 = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v3"))
        for version in (v2, v3, v2):
            await service.activate_version(version.id)

        count = await session.execute(
            select(func.count())
            .select_from(PromptVersion)
            .where(PromptVersion.prompt_id == prompt.id, PromptVersion.is_active)
        )
    assert count.scalar_one() == 1


async def test_prompts_are_scoped_to_their_organization(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        prompt = await _prompt(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await PromptService(session, tenant_b).get_prompt(prompt.id)


async def test_activating_another_orgs_version_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        version = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await PromptService(session, tenant_b).activate_version(version.id)


async def test_default_prompt_contains_the_grounding_rules():
    from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT

    assert "{{company_name}}" in DEFAULT_SALES_SYSTEM_PROMPT
    assert "Never invent" in DEFAULT_SALES_SYSTEM_PROMPT

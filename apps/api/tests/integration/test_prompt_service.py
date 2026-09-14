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
    """Covers activate_version's own bookkeeping across repeated switches.

    This does NOT exercise the partial unique index itself: activate_version
    already serialises deactivate-then-flush-then-activate, so exactly one
    row is active at every step here whether or not the index exists. The
    index is proven separately, by
    test_two_active_versions_in_one_flush_violates_the_partial_unique_index,
    which bypasses activate_version entirely.
    """
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


async def test_two_active_versions_in_one_flush_violates_the_partial_unique_index(tenant_a):
    """Bypasses activate_version's careful statement ordering entirely, to
    prove that the *database* -- not just the service's discipline -- refuses
    two active versions for the same prompt.

    Regression check performed manually while fixing this test: running it
    against a database with `uq_prompt_versions_one_active` dropped
    (`DROP INDEX uq_prompt_versions_one_active;`) makes this test FAIL (the
    flush succeeds with two active rows instead of raising). Recreating the
    index (`CREATE UNIQUE INDEX uq_prompt_versions_one_active ON
    prompt_versions (prompt_id) WHERE is_active;`) makes it pass again. See
    the task report for the verbatim before/after run.
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from app.db.models import PromptVersion

    with pytest.raises(IntegrityError, match="uq_prompt_versions_one_active"):
        async with tenant_session(tenant_a) as session:
            service = PromptService(session, tenant_a)
            prompt = await _prompt(session, tenant_a)
            await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))

            result = await session.execute(
                select(PromptVersion).where(PromptVersion.prompt_id == prompt.id)
            )
            versions = result.scalars().all()
            assert len(versions) == 2
            for version in versions:
                version.is_active = True

            await session.flush()


async def test_created_by_records_the_creating_user(tenant_a):
    async with tenant_session(tenant_a) as session:
        prompt = await _prompt(session, tenant_a)
        version = await PromptService(session, tenant_a).active_version(prompt.id)
    assert version.created_by == tenant_a.user_id


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

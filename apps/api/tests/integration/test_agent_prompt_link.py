import pytest

from app.agents.service import AgentService
from app.chat.service import ChatMessageStart, ChatService
from app.conversations.service import ConversationService
from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.llm.fake_provider import FakeProvider
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _prompt(session, tenant, key="sales_system", text="LINKED MARKER {{company_name}}"):
    return await PromptService(session, tenant).create_prompt(
        CreatePromptInput(name=f"Prompt {key}", key=key, system_prompt=text)
    )


async def test_set_prompt_links_an_agent_to_its_organizations_prompt(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        prompt = await _prompt(session, tenant_a)
        updated = await service.set_prompt(agent.id, prompt.id)
    assert updated.prompt_id == prompt.id


async def test_set_prompt_rejects_another_organizations_prompt(tenant_a, tenant_b):
    async with tenant_session(tenant_b) as session:
        foreign = await _prompt(session, tenant_b, key="foreign")
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        with pytest.raises(NotFoundError):
            await service.set_prompt(agent.id, foreign.id)
        reloaded = await service.get_agent(agent.id)
    assert reloaded.prompt_id is None


async def test_set_prompt_on_another_organizations_agent_is_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_b) as session:
        foreign_agent = await AgentService(session, tenant_b).create_agent(agent_input("Theirs"))
    async with tenant_session(tenant_a) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_a).set_prompt(foreign_agent.id, None)


async def test_linking_reaches_chat_and_records_the_active_version(tenant_a):
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        prompt = await _prompt(session, tenant_a)
        await service.set_prompt(agent.id, prompt.id)
        active = await PromptService(session, tenant_a).active_version(prompt.id)

        chat = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in chat.send(agent.id, "Hello")]

    assert provider.last_request is not None
    assert "LINKED MARKER" in provider.last_request.system
    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].prompt_version_id == active.id


async def test_set_prompt_none_unlinks_and_chat_falls_back_to_default(tenant_a):
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(agent_input("Sales Bot"))
        prompt = await _prompt(session, tenant_a)
        await service.set_prompt(agent.id, prompt.id)
        updated = await service.set_prompt(agent.id, None)

        chat = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in chat.send(agent.id, "Hello")]

    assert updated.prompt_id is None
    assert provider.last_request is not None
    assert "LINKED MARKER" not in provider.last_request.system
    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].prompt_version_id is None


async def test_agents_by_prompt_groups_agents_and_includes_every_requested_id(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        used = await _prompt(session, tenant_a, key="used")
        unused = await _prompt(session, tenant_a, key="unused")
        zed = await service.create_agent(agent_input("Zed Bot"))
        abe = await service.create_agent(agent_input("Abe Bot"))
        await service.create_agent(agent_input("Unlinked Bot"))
        await service.set_prompt(zed.id, used.id)
        await service.set_prompt(abe.id, used.id)

        by_prompt = await service.agents_by_prompt([used.id, unused.id])

    assert [a.name for a in by_prompt[used.id]] == ["Abe Bot", "Zed Bot"]
    assert by_prompt[unused.id] == []

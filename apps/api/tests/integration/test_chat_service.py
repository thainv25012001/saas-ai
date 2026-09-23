import pytest
from sqlalchemy import select

from app.agents.service import AgentService
from app.chat.service import (
    ChatError,
    ChatMessageEnd,
    ChatMessageStart,
    ChatService,
    ChatTextDelta,
)
from app.conversations.service import ConversationService
from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.core.tenancy import tenant_session
from app.db.models import MessageRole, UsageEvent, UsageKind
from app.llm.errors import LLMConfigurationError, LLMUnavailableError
from app.llm.fake_provider import FakeProvider
from app.llm.pricing import estimate_cost
from app.llm.registry import reset_providers
from app.llm.types import Usage
from app.prompts.schemas import CreatePromptInput, CreateVersionInput
from app.prompts.service import PromptService
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


async def _agent(session, tenant, **overrides):
    name = overrides.pop("name", "Sales Bot")
    return await AgentService(session, tenant).create_agent(agent_input(name, **overrides))


async def test_first_message_creates_a_conversation_and_returns_its_id(tenant_a):
    provider = FakeProvider(script=["Hello", " there"])
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hi")]

    starts = [e for e in events if isinstance(e, ChatMessageStart)]
    assert len(starts) == 1
    conversation_id = starts[0].conversation_id

    async with tenant_session(tenant_a) as session:
        conversation = await ConversationService(session, tenant_a).get(conversation_id)
    assert conversation.id == conversation_id


async def test_deltas_concatenate_to_the_fake_providers_script(tenant_a):
    provider = FakeProvider(script=["Hel", "lo!"])
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hi")]

    text = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert text == "Hello!"


async def test_user_and_assistant_messages_are_persisted_with_sequential_seq(tenant_a):
    provider = FakeProvider(script=["Hi there"])
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider)
        starts = [
            e async for e in service.send(agent.id, "Hello") if isinstance(e, ChatMessageStart)
        ]
        conversation_id = starts[0].conversation_id

    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)

    assert [m.role for m in history] == [MessageRole.USER, MessageRole.ASSISTANT]
    assert [m.seq for m in history] == [1, 2]
    assert history[0].content == "Hello"
    assert history[1].content == "Hi there"
    # No prompt was configured on the agent, so the fallback default was
    # used -- prompt_version_id must be None, not a fabricated id.
    assert history[1].prompt_version_id is None


async def test_assistant_message_records_the_estimated_cost(tenant_a):
    usage = Usage(input_tokens=42, output_tokens=17)
    provider = FakeProvider(script=["ok"], usage=usage)
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a, model="claude-sonnet-5")
        service = ChatService(session, tenant_a, provider_override=provider)
        starts = [
            e async for e in service.send(agent.id, "Hello") if isinstance(e, ChatMessageStart)
        ]
        conversation_id = starts[0].conversation_id

    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)

    expected = estimate_cost("claude-sonnet-5", usage)
    assert expected is not None
    assert history[1].cost_usd == expected


async def test_system_prompt_uses_the_active_prompt_version_not_an_older_one(tenant_a):
    """The load-bearing test: proves prompt versioning is actually wired to the
    model, not merely stored in a table. Creates a prompt (version 1), adds and
    activates a second version, and asserts the *v2* text is what the provider
    actually receives."""
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        prompt = await prompts.create_prompt(
            CreatePromptInput(
                name="Sales prompt",
                key="sales",
                system_prompt="v1 text for {{company_name}}",
            )
        )
        v2 = await prompts.create_version(
            prompt.id, CreateVersionInput(system_prompt="v2 UNIQUE MARKER for {{company_name}}")
        )
        await prompts.activate_version(v2.id)

        agent = await _agent(session, tenant_a)
        agent.prompt_id = prompt.id
        await session.flush()

        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hello")]

    assert provider.last_request is not None
    assert "v2 UNIQUE MARKER" in provider.last_request.system
    assert "v1 text" not in provider.last_request.system

    # The persisted assistant message must record *which* version answered --
    # this is the traceability link PHASE-2.md ties Phase 5's evaluation to.
    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].prompt_version_id == v2.id


async def test_company_name_is_substituted_and_no_placeholder_remains(tenant_a):
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider)
        _ = [event async for event in service.send(agent.id, "Hello")]

    assert provider.last_request is not None
    # tenant_a's organization is created by conftest._make_tenant as "Tenant A".
    assert "Tenant A" in provider.last_request.system
    assert "{{" not in provider.last_request.system


async def test_undeclared_template_variable_is_left_untouched(tenant_a):
    """Only declared variables are substituted -- an unknown `{{...}}` must not
    crash the render (str.format would raise on this), and is left as literal
    text rather than silently guessed at."""
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        prompt = await prompts.create_prompt(
            CreatePromptInput(
                name="Custom",
                key="custom",
                system_prompt="Hello {{company_name}}, mystery: {{unknown_var}}",
            )
        )
        agent = await _agent(session, tenant_a)
        agent.prompt_id = prompt.id
        await session.flush()

        service = ChatService(session, tenant_a, provider_override=provider)
        _ = [event async for event in service.send(agent.id, "Hi")]

    assert provider.last_request is not None
    assert "{{unknown_var}}" in provider.last_request.system
    assert "Tenant A" in provider.last_request.system


async def test_second_message_sends_prior_turns_as_history(tenant_a):
    provider1 = FakeProvider(script=["first reply"])
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider1)
        starts = [e async for e in service.send(agent.id, "Hi") if isinstance(e, ChatMessageStart)]
        conversation_id = starts[0].conversation_id

    provider2 = FakeProvider(script=["second reply"])
    async with tenant_session(tenant_a) as session:
        service = ChatService(session, tenant_a, provider_override=provider2)
        _ = [
            event
            async for event in service.send(agent.id, "Follow up", conversation_id=conversation_id)
        ]

    assert provider2.last_request is not None
    texts = [m.text_content for m in provider2.last_request.messages]
    assert texts == ["Hi", "first reply", "Follow up"]


async def test_history_window_limits_the_number_of_prior_turns_sent(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        conversation_id = None
        for user_text, reply in [("m1", "r1"), ("m2", "r2"), ("m3", "r3")]:
            service = ChatService(
                session,
                tenant_a,
                provider_override=FakeProvider(script=[reply]),
                history_window=2,
            )
            starts = [
                e
                async for e in service.send(agent.id, user_text, conversation_id=conversation_id)
                if isinstance(e, ChatMessageStart)
            ]
            conversation_id = starts[0].conversation_id

        provider4 = FakeProvider(script=["r4"])
        service4 = ChatService(session, tenant_a, provider_override=provider4, history_window=2)
        _ = [
            event async for event in service4.send(agent.id, "m4", conversation_id=conversation_id)
        ]

    assert provider4.last_request is not None
    texts = [m.text_content for m in provider4.last_request.messages]
    # Only the last 2 prior turns (m3, r3) plus the new message -- not m1/r1/m2/r2.
    assert texts == ["m3", "r3", "m4"]


async def test_midstream_failure_persists_partial_message_and_yields_chat_error(tenant_a):
    provider = FakeProvider(script=["partial ", "more"], fail_with=LLMUnavailableError("gone"))
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hello")]

    errors = [e for e in events if isinstance(e, ChatError)]
    assert len(errors) == 1
    assert errors[0].code == "llm_unavailable"

    deltas = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert deltas == "partial "

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)

    assert len(history) == 2
    assistant_message = history[1]
    assert assistant_message.error is not None
    assert assistant_message.content == "partial "


async def test_cross_tenant_agent_raises_not_found_before_any_conversation_is_created(
    tenant_a, tenant_b
):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)

    async with tenant_session(tenant_b) as session:
        service = ChatService(session, tenant_b, provider_override=FakeProvider(script=["x"]))
        with pytest.raises(NotFoundError):
            _ = [event async for event in service.send(agent.id, "Hello")]

    async with tenant_session(tenant_a) as session:
        conversations = await ConversationService(session, tenant_a).list_for_agent(agent.id)
    assert conversations == []


async def test_agent_without_a_config_raises_not_found_before_any_conversation_is_created(
    tenant_a,
):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        config = await AgentService(session, tenant_a).get_config(agent.id)
        await session.delete(config)
        await session.flush()

        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["x"]))
        with pytest.raises(NotFoundError):
            _ = [event async for event in service.send(agent.id, "Hello")]

        conversations = await ConversationService(session, tenant_a).list_for_agent(agent.id)
    assert conversations == []


async def test_unpriced_model_records_cost_usd_as_none_not_zero(tenant_a):
    provider = FakeProvider(script=["ok"], usage=Usage(input_tokens=5, output_tokens=5))
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a, model="totally-unpriced-model")
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hello")]
        starts = [e for e in events if isinstance(e, ChatMessageStart)]
        conversation_id = starts[0].conversation_id

    ends = [e for e in events if isinstance(e, ChatMessageEnd)]
    assert len(ends) == 1
    assert ends[0].cost_usd is None

    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].cost_usd is None


async def test_without_a_provider_override_it_resolves_the_provider_via_the_registry(tenant_a):
    """No FakeProvider is injected here -- the agent's own `provider="fake"`
    (the local default, no API key needed) must be what `send()` resolves via
    `registry.get_provider`. This is what actually proves the fallback to the
    registry lookup exists and works, rather than merely being unreachable
    code that every other test bypasses with `provider_override`."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a, provider="fake", model="fake-1")
        service = ChatService(session, tenant_a)
        events = [event async for event in service.send(agent.id, "Hello")]

    deltas = "".join(e.text for e in events if isinstance(e, ChatTextDelta))
    assert deltas != ""


async def test_provider_resolution_happens_before_any_conversation_is_created(
    tenant_a, monkeypatch
):
    """`send()` resolves the provider right after loading the agent/config --
    before the conversation is created or the user message is persisted --
    so a misconfigured provider (a missing API key) never leaves an orphan
    conversation behind. Patches what the registry actually reads rather than
    relying on the ambient environment lacking a key, mirroring
    tests/unit/test_registry.py's own `openai_without_a_key` test.

    Verified by hand that moving the `get_provider(agent.provider)` call in
    `ChatService.send` to after `self._conversations.create(...)` makes this
    test fail: the conversation is created (so `conversations == []` no
    longer holds) before `LLMConfigurationError` is raised. See the task
    report for the verbatim before/after run.
    """
    base = get_settings()
    no_key_settings = base.model_copy(update={"anthropic_api_key": None})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: no_key_settings)
    reset_providers()
    try:
        async with tenant_session(tenant_a) as session:
            agent = await _agent(session, tenant_a, provider="anthropic", model="claude-sonnet-5")
            service = ChatService(session, tenant_a)  # no override: forces the registry lookup
            with pytest.raises(LLMConfigurationError):
                _ = [event async for event in service.send(agent.id, "Hello")]

            conversations = await ConversationService(session, tenant_a).list_for_agent(agent.id)
        assert conversations == []
    finally:
        reset_providers()


async def _usage_events(tenant, conversation_id):
    async with tenant_session(tenant) as session:
        result = await session.execute(
            select(UsageEvent).where(UsageEvent.conversation_id == conversation_id)
        )
        return list(result.scalars().all())


async def test_a_successful_turn_writes_exactly_one_usage_event(tenant_a):
    """PHASE-2.md §5's central requirement -- "every assistant message ...
    writes a `usage_events` row" -- had no chat-level coverage at all:
    deleting the whole `record_usage(...)` call from `ChatService.send` left
    the entire suite green, because `usage_events` was only ever exercised by
    `test_conversation_service.py` calling `record_usage` directly. This is
    the test that fails when that call goes away.
    """
    usage = Usage(input_tokens=42, output_tokens=17)
    provider = FakeProvider(script=["ok"], usage=usage)
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a, model="claude-sonnet-5")
        agent_id = agent.id
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hello")]

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))

    rows = await _usage_events(tenant_a, conversation_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind is UsageKind.LLM
    assert row.agent_id == agent_id
    assert row.provider == "fake"
    assert row.model == "claude-sonnet-5"
    assert row.input_tokens == 42
    assert row.output_tokens == 17
    # The same figure the assistant message and the `message_end` event
    # carry -- a usage row that disagrees with the message it bills for
    # would be worse than none at all.
    assert row.cost_usd == estimate_cost("claude-sonnet-5", usage)
    assert row.cost_usd is not None and row.cost_usd > 0


async def test_pinned_prompt_version_is_used_even_when_a_newer_version_is_active(tenant_a):
    """PHASE-6.md §5: an evaluation run pins a prompt version at start, so a
    version activated mid-run must not change what an already-pinned turn
    sees. Creates v1 (active), then v2 (activated, so v1 is no longer the
    prompt's active version), and asserts a turn pinned to v1 still gets v1's
    text -- and that the persisted message and ChatMessageEnd both record
    v1's id, not v2's."""
    provider = FakeProvider(script=["ok"])
    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        prompt = await prompts.create_prompt(
            CreatePromptInput(
                name="Sales prompt",
                key="sales",
                system_prompt="v1 PINNED MARKER for {{company_name}}",
            )
        )
        v1 = await prompts.active_version(prompt.id)
        v2 = await prompts.create_version(
            prompt.id, CreateVersionInput(system_prompt="v2 UNIQUE MARKER for {{company_name}}")
        )
        await prompts.activate_version(v2.id)

        agent = await _agent(session, tenant_a)
        agent.prompt_id = prompt.id
        await session.flush()

        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hello", prompt_version_id=v1.id)]

    assert provider.last_request is not None
    assert "v1 PINNED MARKER" in provider.last_request.system
    assert "v2 UNIQUE MARKER" not in provider.last_request.system

    ends = [e for e in events if isinstance(e, ChatMessageEnd)]
    assert len(ends) == 1
    assert ends[0].prompt_version_id == v1.id

    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))
    async with tenant_session(tenant_a) as session:
        history = await ConversationService(session, tenant_a).history(conversation_id)
    assert history[1].prompt_version_id == v1.id


async def test_pinning_a_version_from_a_different_prompt_raises_validation_error(tenant_a):
    """`version.prompt_id == agent.prompt_id` is required -- a version from
    some other prompt of the same organization is still not a version of
    THIS agent's prompt, and must be rejected before any conversation row is
    created, exactly like a bad provider or a missing agent."""
    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        agents_prompt = await prompts.create_prompt(
            CreatePromptInput(name="Agent's prompt", key="agents_own", system_prompt="own text")
        )
        other_prompt = await prompts.create_prompt(
            CreatePromptInput(name="Some other prompt", key="other", system_prompt="other text")
        )
        other_version = await prompts.active_version(other_prompt.id)

        agent = await _agent(session, tenant_a)
        agent.prompt_id = agents_prompt.id
        await session.flush()

        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["x"]))
        with pytest.raises(ValidationError):
            _ = [
                event
                async for event in service.send(
                    agent.id, "Hello", prompt_version_id=other_version.id
                )
            ]

        conversations = await ConversationService(session, tenant_a).list_for_agent(agent.id)
    assert conversations == []


async def test_pinning_a_version_on_an_agent_with_no_prompt_raises_validation_error(tenant_a):
    """An agent with `prompt_id is None` uses the fallback default prompt --
    it has no prompt for any version to belong to, so pinning one is always
    a mismatch, not a special case of the active-version lookup."""
    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        prompt = await prompts.create_prompt(
            CreatePromptInput(name="Unrelated prompt", key="unrelated", system_prompt="text")
        )
        version = await prompts.active_version(prompt.id)

        agent = await _agent(session, tenant_a)  # agent.prompt_id is None

        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["x"]))
        with pytest.raises(ValidationError):
            _ = [
                event
                async for event in service.send(agent.id, "Hello", prompt_version_id=version.id)
            ]

        conversations = await ConversationService(session, tenant_a).list_for_agent(agent.id)
    assert conversations == []


async def test_pinning_another_orgs_prompt_version_raises_not_found(tenant_a, tenant_b):
    """A version id from a different organization must 404, not leak whether
    it exists -- the same cross-tenant rule `PromptService.get_version` and
    every other scoped lookup in this codebase already follows."""
    async with tenant_session(tenant_b) as session:
        other_prompts = PromptService(session, tenant_b)
        other_prompt = await other_prompts.create_prompt(
            CreatePromptInput(name="Other org's prompt", key="other_org", system_prompt="text")
        )
        other_version = await other_prompts.active_version(other_prompt.id)

    async with tenant_session(tenant_a) as session:
        prompts = PromptService(session, tenant_a)
        prompt = await prompts.create_prompt(
            CreatePromptInput(name="Tenant A's prompt", key="tenant_a", system_prompt="text")
        )
        agent = await _agent(session, tenant_a)
        agent.prompt_id = prompt.id
        await session.flush()

        service = ChatService(session, tenant_a, provider_override=FakeProvider(script=["x"]))
        with pytest.raises(NotFoundError):
            _ = [
                event
                async for event in service.send(
                    agent.id, "Hello", prompt_version_id=other_version.id
                )
            ]

        conversations = await ConversationService(session, tenant_a).list_for_agent(agent.id)
    assert conversations == []


async def test_a_midstream_failure_writes_no_usage_event(tenant_a):
    """The handled-error path deliberately persists the partial assistant
    message but writes NO usage row: there is no reliable token count for a
    stream that never reached its `message_end`/usage event, and inventing
    one would bill the organization for a number nothing produced."""
    provider = FakeProvider(script=["partial ", "more"], fail_with=LLMUnavailableError("gone"))
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        service = ChatService(session, tenant_a, provider_override=provider)
        events = [event async for event in service.send(agent.id, "Hello")]

    assert any(isinstance(e, ChatError) for e in events)
    conversation_id = next(e.conversation_id for e in events if isinstance(e, ChatMessageStart))

    assert await _usage_events(tenant_a, conversation_id) == []

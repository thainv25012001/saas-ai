from decimal import Decimal

import pytest

from app.agents.schemas import CreateAgentInput
from app.agents.service import AgentService
from app.conversations.schemas import (
    AppendMessageInput,
    CreateConversationInput,
    RecordUsageInput,
)
from app.conversations.service import ConversationService
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import tenant_session
from app.db.models import ConversationChannel, ConversationMessage, MessageRole, UsageKind

pytestmark = pytest.mark.anyio


async def _agent(session, tenant):
    return await AgentService(session, tenant).create_agent(CreateAgentInput(name="Sales Bot"))


async def _conversation(session, tenant, agent_id=None):
    if agent_id is None:
        agent = await _agent(session, tenant)
        agent_id = agent.id
    return await ConversationService(session, tenant).create(
        agent_id, CreateConversationInput(channel=ConversationChannel.PLAYGROUND)
    )


async def test_create_returns_a_conversation_scoped_to_the_tenant(tenant_a):
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)
    assert conversation.organization_id == tenant_a.organization_id


async def test_get_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ConversationService(session, tenant_b).get(conversation.id)


async def test_create_for_an_agent_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    """create() must check the agent's tenant itself: with RLS alone, the
    agent lookup silently sees zero rows for a foreign id, and it is this
    explicit check, not RLS, that turns that into a NotFoundError instead of
    creating a conversation no agent in this tenant owns."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ConversationService(session, tenant_b).create(
                agent.id, CreateConversationInput(channel=ConversationChannel.PLAYGROUND)
            )


async def test_append_message_with_a_colliding_seq_raises_conflict(tenant_a):
    """`next_seq`'s `SELECT MAX(seq) + 1` is a best-effort computation, not a
    lock: two concurrent appends can compute the same next value. The
    `(conversation_id, seq)` unique constraint is the real backstop, and
    `append_message` must translate the resulting `IntegrityError` into
    `ConflictError` rather than letting it propagate raw.

    Forces the collision deterministically — by making `next_seq` return an
    already-used value — rather than relying on a genuine race under test,
    which would be flaky. Verified by hand that removing the `try/except`
    around the flush in `append_message` makes this fail (with a raw
    `IntegrityError` instead of `ConflictError`), and that every other test
    still passes: this branch is not free to delete."""
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        conversation = await _conversation(session, tenant_a)
        await service.append_message(
            conversation.id, AppendMessageInput(role=MessageRole.USER, content="first")
        )

        async def _always_seq_one(_conversation_id: object) -> int:
            return 1

        service.next_seq = _always_seq_one  # type: ignore[method-assign]

        with pytest.raises(ConflictError):
            await service.append_message(
                conversation.id, AppendMessageInput(role=MessageRole.USER, content="collides")
            )


async def test_append_message_assigns_sequential_seq(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        conversation = await _conversation(session, tenant_a)
        first = await service.append_message(
            conversation.id, AppendMessageInput(role=MessageRole.USER, content="hello")
        )
        second = await service.append_message(
            conversation.id, AppendMessageInput(role=MessageRole.ASSISTANT, content="hi there")
        )
    assert first.seq == 1
    assert second.seq == 2


async def test_history_orders_by_seq_not_by_insertion_order(tenant_a):
    """Insert two rows directly, seq 2 before seq 1, bypassing
    append_message's own allocation. If history() ordered by insertion
    (e.g. primary key / created_at) rather than the `seq` column, this
    would come back reversed."""
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        conversation = await _conversation(session, tenant_a)

        later = ConversationMessage(
            id=uuid7(),
            organization_id=tenant_a.organization_id,
            conversation_id=conversation.id,
            seq=2,
            role=MessageRole.ASSISTANT,
            content="second",
        )
        earlier = ConversationMessage(
            id=uuid7(),
            organization_id=tenant_a.organization_id,
            conversation_id=conversation.id,
            seq=1,
            role=MessageRole.USER,
            content="first",
        )
        session.add_all([later, earlier])
        await session.flush()

        history = await service.history(conversation.id)
    assert [m.content for m in history] == ["first", "second"]


async def test_history_with_a_limit_returns_the_most_recent_messages_in_ascending_order(tenant_a):
    """The windowing the chat service depends on: `ORDER BY seq LIMIT N`
    would return the *oldest* N, silently handing the model stale context.
    Uses more messages than the limit, or this proves nothing."""
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        conversation = await _conversation(session, tenant_a)
        for i in range(1, 6):  # seq 1..5
            await service.append_message(
                conversation.id, AppendMessageInput(role=MessageRole.USER, content=f"msg-{i}")
            )

        recent = await service.history(conversation.id, limit=2)
    assert [m.content for m in recent] == ["msg-4", "msg-5"]
    assert [m.seq for m in recent] == [4, 5]


async def test_cost_usd_none_round_trips_as_null(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        conversation = await _conversation(session, tenant_a)
        message = await service.append_message(
            conversation.id,
            AppendMessageInput(role=MessageRole.ASSISTANT, content="unpriced", cost_usd=None),
        )
    assert message.cost_usd is None


async def test_cost_usd_round_trips_as_an_exact_decimal(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        conversation = await _conversation(session, tenant_a)
        message = await service.append_message(
            conversation.id,
            AppendMessageInput(
                role=MessageRole.ASSISTANT,
                content="priced",
                cost_usd=Decimal("0.001234"),
            ),
        )
    assert message.cost_usd == Decimal("0.001234")


async def test_list_for_agent_from_another_tenant_returns_empty(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
        await _conversation(session, tenant_a, agent_id=agent.id)
    async with tenant_session(tenant_b) as session:
        conversations = await ConversationService(session, tenant_b).list_for_agent(agent.id)
    assert conversations == []


async def test_record_usage_writes_a_row_scoped_to_the_tenant(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = ConversationService(session, tenant_a)
        agent = await _agent(session, tenant_a)
        conversation = await _conversation(session, tenant_a, agent_id=agent.id)
        event = await service.record_usage(
            RecordUsageInput(
                agent_id=agent.id,
                conversation_id=conversation.id,
                kind=UsageKind.LLM,
                provider="openai",
                model="gpt-4o-mini",
                input_tokens=100,
                output_tokens=50,
                cost_usd=Decimal("0.000123"),
            )
        )
    assert event.organization_id == tenant_a.organization_id
    assert event.input_tokens == 100
    assert event.cost_usd == Decimal("0.000123")


async def test_record_usage_with_an_agent_from_another_tenant_raises_not_found(tenant_a, tenant_b):
    """Without this check, record_usage would happily INSERT a usage_events
    row whose agent_id FK genuinely references another org's agent: the FK
    constraint is enforced with elevated privileges and does not consult
    this session's RLS policy, so it would not stop the write. RLS still
    hides the resulting row from tenant_b afterwards, but the accounting
    substrate would be left holding a cross-tenant reference no query ever
    validated."""
    async with tenant_session(tenant_a) as session:
        agent = await _agent(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ConversationService(session, tenant_b).record_usage(
                RecordUsageInput(
                    agent_id=agent.id,
                    kind=UsageKind.LLM,
                    provider="openai",
                    model="gpt-4o-mini",
                )
            )


async def test_record_usage_with_a_conversation_from_another_tenant_raises_not_found(
    tenant_a, tenant_b
):
    async with tenant_session(tenant_a) as session:
        conversation = await _conversation(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await ConversationService(session, tenant_b).record_usage(
                RecordUsageInput(
                    conversation_id=conversation.id,
                    kind=UsageKind.LLM,
                    provider="openai",
                    model="gpt-4o-mini",
                )
            )

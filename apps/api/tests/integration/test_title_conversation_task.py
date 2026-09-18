"""`title_conversation_task` -- the background job that gives a conversation
a readable label.

The job costs money every time it calls a provider, so most of what these
tests pin is when it must *not* call one.
"""

import uuid

import pytest

from app.agents.service import AgentService
from app.conversations.schemas import AppendMessageInput, CreateConversationInput
from app.conversations.service import ConversationService
from app.conversations.titles import UNTITLED
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ConversationChannel, MembershipRole, MessageRole
from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider
from app.workers import tasks as worker_tasks
from tests.factories import agent_input

pytestmark = pytest.mark.anyio


class _ExplodingProvider:
    """Fails the test if the job calls a provider at all.

    This is the guard on the bill: the difference between titling a
    conversation once and titling it on every turn is one `title IS NULL`
    check, and nothing else in this suite would notice it going missing.
    """

    name = "fake"

    async def generate(self, request):  # noqa: ANN001, ANN201
        raise AssertionError("the provider must not be called for an already-titled conversation")

    def stream(self, request):  # noqa: ANN001, ANN201
        raise AssertionError("the provider must not be called for an already-titled conversation")


def _failing_provider() -> FakeProvider:
    """`fail_with` is what `FakeProvider` already offers for this -- the same
    idiom `test_chat_endpoint.py` uses. A hand-rolled stub would drift from
    the `LLMProvider` protocol as it grows. The script is unused and only has
    to be non-empty, which `FakeProvider` enforces at construction."""
    return FakeProvider(script=["unused"], fail_with=LLMUnavailableError("upstream is down"))


def _tenant(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=MembershipRole.OWNER, request_id="test"
    )


async def _seed(
    org_id: uuid.UUID,
    *,
    user_text: str = "how much is the starter plan?",
    assistant_text: str | None = "Forty dollars a month.",
    title: str | None = None,
    channel: ConversationChannel = ConversationChannel.PLAYGROUND,
) -> uuid.UUID:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        agent = await AgentService(session, tenant).create_agent(agent_input("Sales Bot"))
        service = ConversationService(session, tenant)
        conversation = await service.create(agent.id, CreateConversationInput(channel=channel))
        if title is not None:
            conversation.title = title
        if user_text:
            await service.append_message(
                conversation.id, AppendMessageInput(role=MessageRole.USER, content=user_text)
            )
        if assistant_text is not None:
            await service.append_message(
                conversation.id,
                AppendMessageInput(role=MessageRole.ASSISTANT, content=assistant_text),
            )
    return conversation.id


async def _title_of(org_id: uuid.UUID, conversation_id: uuid.UUID) -> str | None:
    tenant = _tenant(org_id)
    async with tenant_session(tenant) as session:
        conversation = await ConversationService(session, tenant).get(conversation_id)
        return conversation.title


def _use_provider(monkeypatch, provider) -> None:  # noqa: ANN001
    monkeypatch.setattr(worker_tasks, "get_provider", lambda _name: provider)


async def test_writes_the_generated_title(tenant_a, monkeypatch):
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id)
    _use_provider(monkeypatch, FakeProvider(script=["Starter plan pricing"]))

    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) == "Starter plan pricing"


async def test_an_already_titled_conversation_never_reaches_the_provider(tenant_a, monkeypatch):
    """The guard that keeps this one call per conversation rather than one
    per turn."""
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id, title="Already named")
    _use_provider(monkeypatch, _ExplodingProvider())

    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) == "Already named"


async def test_a_provider_failure_falls_back_to_the_first_user_message(tenant_a, monkeypatch):
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id)
    _use_provider(monkeypatch, _failing_provider())

    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) == "how much is the starter plan?"


async def test_an_empty_answer_falls_back_rather_than_storing_a_blank(tenant_a, monkeypatch):
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id)
    _use_provider(monkeypatch, FakeProvider(script=["   "]))

    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) == "how much is the starter plan?"


async def test_the_fallback_is_a_real_write_so_the_job_cannot_recur(tenant_a, monkeypatch):
    """A fallback that left `title` NULL would be re-enqueued and re-billed
    on the next turn, forever, for exactly the conversations whose provider
    is broken."""
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id)
    _use_provider(monkeypatch, _failing_provider())
    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    _use_provider(monkeypatch, _ExplodingProvider())
    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) is not None


async def test_a_conversation_with_no_messages_is_left_untitled(tenant_a, monkeypatch):
    """The job is enqueued after the turn's transaction commits, but a lost
    race must be a retry, not a title invented from an empty conversation."""
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id, user_text="", assistant_text=None)
    _use_provider(monkeypatch, _ExplodingProvider())

    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) is None


async def test_a_whitespace_only_question_still_gets_a_label(tenant_a, monkeypatch):
    org_id = tenant_a.organization_id
    conversation_id = await _seed(org_id, user_text="   ")
    _use_provider(monkeypatch, _failing_provider())

    await worker_tasks.title_conversation_task(
        {}, organization_id=str(org_id), conversation_id=str(conversation_id)
    )

    assert await _title_of(org_id, conversation_id) == UNTITLED


async def test_a_conversation_from_another_organization_is_not_touched(tenant_a, tenant_b):
    """The job carries an organization id and opens its own tenant session
    with it, like `ingest_document_task`. A job enqueued (or tampered with)
    against the wrong organization must find no row rather than retitle
    someone else's conversation."""
    conversation_id = await _seed(tenant_a.organization_id)

    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await worker_tasks.title_conversation_task(
            {},
            organization_id=str(tenant_b.organization_id),
            conversation_id=str(conversation_id),
        )

    assert await _title_of(tenant_a.organization_id, conversation_id) is None

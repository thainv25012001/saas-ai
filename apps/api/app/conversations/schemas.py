from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.db.models import ConversationChannel, MessageRole, UsageKind


class CreateConversationInput(BaseModel):
    channel: ConversationChannel
    visitor_id: str | None = None


class AppendMessageInput(BaseModel):
    # None means "let the service generate one" (every caller before Task 6).
    # The chat service pre-generates this id so it can hand the assistant's
    # message id to the caller in a `message_start` event *before* the
    # message's content is known, then persist under that same id once
    # streaming finishes -- so the id a client was told to expect is the id
    # the row actually gets.
    id: UUID | None = None
    role: MessageRole
    content: str | None = None
    content_blocks: list[dict[str, Any]] | None = None
    prompt_version_id: UUID | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # None, not 0: an unpriced model must round-trip as NULL. See
    # app/llm/pricing.py's estimate_cost.
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    finish_reason: str | None = None
    error: str | None = None


class RecordUsageInput(BaseModel):
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    kind: UsageKind
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal | None = None

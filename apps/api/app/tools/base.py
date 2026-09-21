"""The tool abstraction every Phase 4 tool (and later, MCP adapter) implements.

Per `docs/ARCHITECTURE.md` §7.1: a tool declares its arguments as a Pydantic
model and its behaviour as `execute`; `ToolRegistry` (see `registry.py`) turns
the model into the JSON schema a provider is shown and runs `execute` behind
the argument-validation, timeout and error handling that keep a tool's own
mistake from ending the conversation.
"""

import uuid
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field

from app.rag.retrieve import CitationPayload


class ToolContext(BaseModel):
    """Who is asking, and on whose behalf -- threaded into every tool call.

    Every field here is resolved by the server before a tool ever runs, the
    same way `app.core.tenancy.TenantContext` is: from the authenticated
    request, or from the conversation's agent. None of them is an
    `args_model` field, and that split is deliberate, not incidental -- a
    tool's arguments come from the model's (possibly adversarial, e.g. an
    untrusted chat-widget visitor's) turn, while this context does not.
    `ToolRegistry.register` enforces the sharpest case of this,
    `organization_id`, by refusing to register a tool whose `args_model`
    declares that field: a model must have no argument through which it can
    ask for another tenant's data.
    """

    organization_id: uuid.UUID
    agent_id: uuid.UUID
    conversation_id: uuid.UUID
    request_id: str
    visitor_id: str | None = None


class ToolResult(BaseModel):
    """What a tool hands back: to the model, on its next turn, and to the
    chat layer, for the SSE stream and the persisted transcript.

    `content` is the only field every provider sees; `data` is the
    structured half of the split `docs/ARCHITECTURE.md` §7.1 describes --
    e.g. a product card the playground can render richly while the model
    still reads a compact text summary of the same result. `citations`
    reuses `app.rag.retrieve.CitationPayload`, the same SSE-facing shape
    `ChatService` already emits for RAG grounding, rather than a separate
    `CitationRef` type: a tool result and a chat turn report a grounding
    chunk identically, with nothing left to translate between them.

    `duration_ms` is `None` from every tool's own `execute` -- no tool body
    times itself. It is filled in by whatever dispatches the call
    (`app/chat/service.py`'s `_LockedSessionTool`, which wraps each call in
    an `asyncio.Lock` -- every Phase 4 builtin shares one `AsyncSession` per
    turn, which is not safe for concurrent use, so calls serialise on that
    lock rather than each opening a session of its own -- and already
    brackets the call in a `time.monotonic()` pair to do it) and persisted
    onto `MessageToolCall.duration_ms`, §3.6's declared column for it. It
    measures the call's full wall-clock time, including any wait for a
    sibling call already holding the lock -- see `_LockedSessionTool`'s own
    docstring for why that wait is deliberately still counted here even
    though a *separate* budget (`_LockedSessionTool._own_timeout_seconds`)
    is what decides whether the call is treated as having timed out.
    """

    content: str
    data: dict[str, Any] | None = None
    citations: list[CitationPayload] = Field(default_factory=list)
    is_error: bool = False
    duration_ms: int | None = None


class AgentTool(ABC):
    """One capability the agent can invoke.

    A subclass supplies `name`, `description` and `args_model` as class
    attributes and implements `execute`; `ToolRegistry.specs_for` derives the
    provider-facing JSON schema from `args_model.model_json_schema()`
    directly, so the schema the model is shown can never drift from the
    validation `ToolRegistry.execute` actually runs against the same model.
    """

    name: str
    description: str
    args_model: type[BaseModel]

    #: Per `docs/ARCHITECTURE.md` §7.3: "per-tool, default 10s". Overridable
    #: per subclass for a tool known to be slower (or that must fail faster).
    timeout_seconds: float = 10.0

    @abstractmethod
    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Run the tool. `args` is already validated against `args_model` --
        `ToolRegistry.execute` is what performs that validation and turns a
        `pydantic.ValidationError` into an error `ToolResult` before this is
        ever called, so an implementation can read `args`' fields directly.
        """

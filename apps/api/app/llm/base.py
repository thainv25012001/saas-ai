from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    StreamEvent,
    ThinkingStyle,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What a specific model will actually accept.

    This exists because the current Claude models REJECT `temperature` with a 400,
    while `agents.temperature` is a column in our schema with a dashboard input behind
    it. Without per-model capabilities, every Anthropic agent would fail on its first
    message. Capabilities are data so adding a model is a table entry, not a branch.
    """

    supports_sampling: bool
    supports_thinking: bool
    thinking_style: ThinkingStyle
    supports_effort: bool
    max_output_tokens: int


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def capabilities(self, model: str) -> ModelCapabilities: ...

    async def generate(self, request: CompletionRequest) -> CompletionResponse: ...

    # Deliberately not `async def`: returns the iterator directly so callers write
    # `async for event in provider.stream(req)` with no extra await.
    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...

    async def generate_structured(
        self, request: CompletionRequest, schema: type[SchemaT]
    ) -> SchemaT: ...

"""LLM provider test doubles shared by the unit and integration suites."""

from collections.abc import AsyncIterator

from app.llm.fake_provider import FakeProvider
from app.llm.types import CompletionRequest, StreamEvent

#: The model `AnsweredByAnotherModel` reports in place of the requested one.
FALLBACK_MODEL = "fallback/m"


class AnsweredByAnotherModel(FakeProvider):
    """Reports a different model on its events than the request asked for,
    as `OpenRouterProvider` does after falling back from a rejected one."""

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        async for event in super()._stream(request.model_copy(update={"model": FALLBACK_MODEL})):
            yield event

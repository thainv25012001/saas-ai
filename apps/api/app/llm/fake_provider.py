from collections.abc import AsyncIterator

from app.llm.base import ModelCapabilities, SchemaT
from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    Usage,
    UsageEvent,
)

_DEFAULT_SCRIPT = [
    "Thanks for asking! ",
    "I am a placeholder response ",
    "until a real provider is configured.",
]


class FakeProvider:
    """A deterministic provider with no network calls.

    This is production code, not a test fixture, for two reasons. Every test in this
    phase runs against it, so the suite is fast, free and reproducible for anyone who
    clones the repo without an API key — a suite that only runs for people with billing
    configured stops being run. And it is what `ENVIRONMENT=local` falls back to when no
    key is set, so the playground works out of the box.
    """

    name = "fake"

    def __init__(
        self,
        script: list[str] | None = None,
        usage: Usage | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._script = script if script is not None else list(_DEFAULT_SCRIPT)
        self._usage = usage or Usage(input_tokens=10, output_tokens=5)
        if fail_with is not None and not self._script:
            # The documented contract is "raises after at least one chunk has
            # streamed" — that is the whole point of `fail_with` (Task 6 needs
            # to reproduce tokens already reaching the browser before the
            # provider dies). An empty script has no chunk to fail after, so
            # this would either silently fail before any output or raise from
            # `generate()` with nothing to distinguish it from any other
            # failure mode. Reject it at construction instead of yielding a
            # contract violation later.
            raise ValueError("fail_with requires a non-empty script")
        self._fail_with = fail_with
        self.last_request: CompletionRequest | None = None

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        self.last_request = request
        if self._fail_with is not None:
            raise self._fail_with
        return CompletionResponse(
            content=[TextBlock(text="".join(self._script))],
            usage=self._usage,
            model=request.model,
            stop_reason="end_turn",
        )

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.last_request = request
        yield MessageStartEvent(model=request.model)
        for index, chunk in enumerate(self._script):
            # Fail AFTER the first chunk so callers can reproduce the case where
            # output has already reached the browser before the provider dies.
            if self._fail_with is not None and index == 1:
                raise self._fail_with
            yield TextDeltaEvent(text=chunk)
        if self._fail_with is not None and len(self._script) <= 1:
            raise self._fail_with
        yield UsageEvent(usage=self._usage)
        yield MessageEndEvent(stop_reason="end_turn", usage=self._usage, model=request.model)

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def generate_structured(
        self, request: CompletionRequest, schema: type[SchemaT]
    ) -> SchemaT:
        self.last_request = request
        return schema()

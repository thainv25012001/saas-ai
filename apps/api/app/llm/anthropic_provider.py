from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.core.logging import get_logger
from app.llm.base import ModelCapabilities, SchemaT
from app.llm.errors import (
    LLMConfigurationError,
    LLMRateLimitError,
    LLMUnavailableError,
)
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

logger = get_logger(__name__)

# Sampling was REMOVED from these models: sending `temperature` returns a 400.
# Thinking is adaptive and on by default; depth is set with output_config.effort.
_NO_SAMPLING = ModelCapabilities(
    supports_sampling=False,
    supports_thinking=True,
    thinking_style="adaptive",
    supports_effort=True,
    max_output_tokens=64_000,
)

# Older models keep the classic sampling + explicit thinking-budget surface.
_CLASSIC = ModelCapabilities(
    supports_sampling=True,
    supports_thinking=True,
    thinking_style="budget",
    supports_effort=False,
    max_output_tokens=8_192,
)

_CAPABILITIES: dict[str, ModelCapabilities] = {
    "claude-opus-5": _NO_SAMPLING,
    "claude-sonnet-5": _NO_SAMPLING,
    "claude-opus-4-8": _NO_SAMPLING,
    "claude-haiku-4-5": _CLASSIC,
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def capabilities(self, model: str) -> ModelCapabilities:
        # An unknown model gets the CONSERVATIVE set, not the permissive one.
        # Guessing that a new model accepts sampling produces a 400 on first use;
        # guessing it does not merely sends less than we could have.
        return _CAPABILITIES.get(model, _NO_SAMPLING)

    def _build_kwargs(self, request: CompletionRequest) -> dict[str, Any]:
        caps = self.capabilities(request.model)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": min(request.max_tokens, caps.max_output_tokens),
            # Anthropic takes the system prompt top-level, NOT as a message.
            "system": request.system,
            "messages": [
                {"role": m.role, "content": m.text_content}
                for m in request.messages
                if m.role != "system"
            ],
        }

        if request.temperature is not None:
            if caps.supports_sampling:
                kwargs["temperature"] = request.temperature
            else:
                logger.debug(
                    "dropped_unsupported_parameter",
                    parameter="temperature",
                    model=request.model,
                )

        if caps.supports_thinking and caps.thinking_style == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}

        if request.effort is not None and caps.supports_effort:
            kwargs["output_config"] = {"effort": request.effort}

        return kwargs

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        kwargs = self._build_kwargs(request)
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                yield MessageStartEvent(model=request.model)
                async for raw_event in stream:
                    # The SDK's real stream yields a large discriminated union of
                    # event types (content block start/stop, citations, thinking,
                    # signature, …) that only `content_block_delta` /
                    # `text_delta` events carry a `.delta.text` on. Narrowing that
                    # union member-by-member would buy nothing here — the shape
                    # is checked at runtime by the `type` comparisons below — so
                    # treat it as `Any` rather than fight mypy over an SDK type
                    # this module does not otherwise care about.
                    event: Any = raw_event
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        yield TextDeltaEvent(text=event.delta.text)

                final = await stream.get_final_message()
                usage = Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                )
                yield UsageEvent(usage=usage)
                yield MessageEndEvent(
                    stop_reason=final.stop_reason, usage=usage, model=request.model
                )
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError("the model provider is rate limiting us") from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailableError("could not reach the model provider") from exc
        except anthropic.APIStatusError as exc:
            # 401/403 is an operator problem (bad or missing key), not a user one.
            if exc.status_code in (401, 403):
                raise LLMConfigurationError("the model provider rejected our credentials") from exc
            raise LLMUnavailableError(f"the model provider returned {exc.status_code}") from exc

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        parts: list[str] = []
        usage = Usage()
        stop_reason: str | None = None
        async for event in self._stream(request):
            if event.type == "text_delta":
                parts.append(event.text)
            elif event.type == "message_end":
                usage, stop_reason = event.usage, event.stop_reason
        return CompletionResponse(
            content=[TextBlock(text="".join(parts))],
            usage=usage,
            model=request.model,
            stop_reason=stop_reason,
        )

    async def generate_structured(
        self, request: CompletionRequest, schema: type[SchemaT]
    ) -> SchemaT:
        raise NotImplementedError("structured output arrives with Phase 4 tool calling")

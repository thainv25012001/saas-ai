from collections.abc import AsyncIterator
from typing import Any

import anthropic
from anthropic.types import RawContentBlockDeltaEvent

from app.core.logging import get_logger
from app.llm.base import ModelCapabilities, SchemaT
from app.llm.errors import (
    LLMConfigurationError,
    LLMEmptyResponseError,
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
# Output ceiling is the documented 128K for this generation (verified against
# Anthropic's public model docs 2026-09-14, not `client.models.retrieve()` —
# that would be authoritative but needs a live API key this module does not
# have at capability-table-authoring time).
_NO_SAMPLING = ModelCapabilities(
    supports_sampling=False,
    supports_thinking=True,
    thinking_style="adaptive",
    supports_effort=True,
    max_output_tokens=128_000,
)

# Older models keep the classic sampling surface. `supports_thinking` is
# honestly False here rather than claiming a "budget" style the code below
# never implements: extended thinking for these models is driven by a
# `budget_tokens` parameter, and sending that to ANY model in our table is
# exactly the mistake this capability table exists to prevent. If budget-style
# thinking is ever wired up, this record is where that support gets declared.
# Documented output ceiling for this generation is 64K.
_CLASSIC = ModelCapabilities(
    supports_sampling=True,
    supports_thinking=False,
    thinking_style="none",
    supports_effort=False,
    max_output_tokens=64_000,
)

# `stop_reason` values that legitimately end a response carrying no text.
# Verified against the installed SDK, not assumed: `anthropic.types.StopReason`
# is `Literal["end_turn", "max_tokens", "stop_sequence", "tool_use",
# "pause_turn", "refusal", "model_context_window_exceeded"]`.
#
#   tool_use -- the model answered by calling a tool instead of writing prose.
#   max_tokens -- the output budget ran out. This one is not hypothetical on
#     our own default: `claude-opus-5` runs adaptive thinking on by default,
#     so a reasoning-heavy turn can spend the entire budget on hidden
#     reasoning tokens before emitting any visible content. The turn is
#     truncated, not empty; reporting it as "the model returned nothing"
#     reads as a refusal and sends whoever is debugging it looking in the
#     wrong place, when the actual fix is a larger `max_tokens`.
#
# `refusal` is deliberately NOT here: a refusal with no text is exactly the
# case PHASE-2.md §3's row is about. Nor is `model_context_window_exceeded`,
# which is a request that was too large to run at all rather than a turn that
# ran and produced nothing.
_NO_TEXT_EXPECTED_STOP_REASONS = frozenset({"tool_use", "max_tokens"})

_CAPABILITIES: dict[str, ModelCapabilities] = {
    "claude-opus-5": _NO_SAMPLING,
    "claude-sonnet-5": _NO_SAMPLING,
    "claude-opus-4-8": _NO_SAMPLING,
    "claude-opus-4-7": _NO_SAMPLING,
    "claude-opus-4-6": _CLASSIC,
    "claude-sonnet-4-6": _CLASSIC,
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

        max_tokens = min(request.max_tokens, caps.max_output_tokens)
        if max_tokens < request.max_tokens:
            logger.debug(
                "clamped_max_tokens",
                requested=request.max_tokens,
                allowed=caps.max_output_tokens,
                model=request.model,
            )

        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": max_tokens,
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
                emitted_text = False
                async for raw_event in stream:
                    # `RawContentBlockDeltaEvent` is the single member of the
                    # SDK's stream-event union that carries `.delta.text` — the
                    # isinstance check narrows `.delta` (itself a discriminated
                    # union) down to `TextDelta` once `.type == "text_delta"`,
                    # so this is fully typed with no `Any` and no `# type:
                    # ignore`. That matters beyond tidiness: if a discriminator
                    # string is ever renamed upstream, this comparison simply
                    # stops matching — silently, with no exception — and a
                    # paid, billed response would come back with empty text.
                    # The `emitted_text` check below is the runtime tripwire
                    # for that same failure; this isinstance check is the
                    # compile-time one mypy can still catch if the SDK's type
                    # itself changes shape.
                    if (
                        isinstance(raw_event, RawContentBlockDeltaEvent)
                        and raw_event.delta.type == "text_delta"
                    ):
                        emitted_text = True
                        yield TextDeltaEvent(text=raw_event.delta.text)

                final = await stream.get_final_message()
                usage = Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                )
                if not emitted_text and final.stop_reason not in _NO_TEXT_EXPECTED_STOP_REASONS:
                    # The stream completed without a single text delta, and
                    # not for any of the reasons that legitimately produce
                    # none. PHASE-2.md
                    # §3 names this exact case ("model refused or returned
                    # nothing") and the domain error it must become --
                    # previously it produced a `message_end`, an empty
                    # assistant row, a `usage_events` row and a reported
                    # success, which is a paid request silently returning
                    # nothing. Logged as well as raised, because the likeliest
                    # cause is our own loop: a discriminator string above no
                    # longer matching what the SDK actually sends.
                    logger.warning(
                        "llm_empty_response",
                        model=request.model,
                        output_tokens=usage.output_tokens,
                        stop_reason=final.stop_reason,
                    )
                    raise LLMEmptyResponseError("the model returned no text")
                yield UsageEvent(usage=usage)
                yield MessageEndEvent(
                    stop_reason=final.stop_reason, usage=usage, model=request.model
                )
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError("the model provider is rate limiting us") from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailableError("could not reach the model provider") from exc
        except anthropic.APIStatusError as exc:
            # 401/403 is an operator problem (bad or missing key). Any other
            # 4xx (400, 404, 422, …) means we sent a request the model will
            # never accept — e.g. a capability-table miss letting an
            # unsupported parameter through, which is the exact failure this
            # whole provider exists to prevent. Neither is transient:
            # retrying resends the identical bad request. Only a 5xx is
            # actually "try again later".
            if 400 <= exc.status_code < 500:
                if exc.status_code in (401, 403):
                    raise LLMConfigurationError(
                        "the model provider rejected our credentials"
                    ) from exc
                raise LLMConfigurationError(
                    f"the model provider rejected our request ({exc.status_code})"
                ) from exc
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

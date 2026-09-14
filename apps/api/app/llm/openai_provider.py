from collections.abc import AsyncIterator

import openai
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionUserMessageParam,
)

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

# Unlike Anthropic, OpenAI did not remove sampling from any current chat
# model, and there is no per-model "thinking" surface on the Chat Completions
# API this provider targets. One permissive record is correct for every
# model name we might be given; if a future model needs a different record,
# this becomes a table exactly like the Anthropic one.
_CAPABILITIES = ModelCapabilities(
    supports_sampling=True,
    supports_thinking=False,
    thinking_style="none",
    supports_effort=False,
    max_output_tokens=16_384,
)


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key)

    def capabilities(self, model: str) -> ModelCapabilities:
        return _CAPABILITIES

    def _messages(self, request: CompletionRequest) -> list[ChatCompletionMessageParam]:
        # OpenAI has no top-level system parameter — the mirror image of
        # Anthropic, which takes the system prompt as a top-level `system`
        # parameter and rejects a role=system message. Here the system
        # prompt IS a message, placed first. Getting this backwards means
        # one of the two providers silently ignores the agent's persona.
        messages: list[ChatCompletionMessageParam] = [
            ChatCompletionSystemMessageParam(role="system", content=request.system)
        ]
        for message in request.messages:
            if message.role == "user":
                messages.append(
                    ChatCompletionUserMessageParam(role="user", content=message.text_content)
                )
            elif message.role == "assistant":
                messages.append(
                    ChatCompletionAssistantMessageParam(
                        role="assistant", content=message.text_content
                    )
                )
            # A "system" entry inside `request.messages` (there shouldn't be
            # one — `request.system` is the one true source) is dropped
            # rather than duplicated as a second system message.
        return messages

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        messages = self._messages(request)
        try:
            # `AsyncCompletions.create` is `@overload`ed on the LITERAL value
            # of `stream=`. Building one `dict[str, Any]` of kwargs and
            # calling `create(**kwargs)` — the natural mirror of the
            # Anthropic provider's `_build_kwargs` — collapses the whole
            # call, and therefore every `chunk` in the loop below, to `Any`
            # (verified with `reveal_type` against the installed SDK: even a
            # `dict[str, float]` spread for the optional `temperature` alone
            # is enough to erase the overload). Two literal call sites is the
            # price of keeping this fully typed instead of reaching for
            # `Any` or an `isinstance` check on the SDK's internal streaming
            # class — the latter would only be reachable by construction
            # here anyway, since `stream=True` is always literally set.
            if request.temperature is not None:
                stream = await self._client.chat.completions.create(
                    model=request.model,
                    messages=messages,
                    max_tokens=request.max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    temperature=request.temperature,
                )
            else:
                stream = await self._client.chat.completions.create(
                    model=request.model,
                    messages=messages,
                    max_tokens=request.max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                )

            yield MessageStartEvent(model=request.model)
            emitted_text = False
            usage = Usage()
            finish_reason: str | None = None
            async for chunk in stream:
                # The usage-only final chunk (sent because we asked for
                # `stream_options.include_usage`) has an EMPTY `choices`
                # list; indexing into it unconditionally raises IndexError
                # on every single request.
                if chunk.choices:
                    choice = chunk.choices[0]
                    content = choice.delta.content
                    # A role-only opening delta carries `content=None`; an
                    # empty string is just as much "no text" as `None` is.
                    if content:
                        emitted_text = True
                        yield TextDeltaEvent(text=content)
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
                if chunk.usage is not None:
                    usage = Usage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                    )

            if usage.output_tokens > 0 and not emitted_text:
                # We were billed for output tokens but never emitted a single
                # text delta. This should be impossible; if it happens either
                # the model returned a content type this loop does not
                # recognize (e.g. a tool call with no text) or a field name
                # above no longer matches what the SDK actually sends. Either
                # way this is a paid request returning empty text with no
                # exception, so it must be visible in logs.
                logger.warning(
                    "no_text_delta_despite_output_tokens",
                    model=request.model,
                    output_tokens=usage.output_tokens,
                )
            yield UsageEvent(usage=usage)
            yield MessageEndEvent(stop_reason=finish_reason, usage=usage, model=request.model)
        except openai.RateLimitError as exc:
            raise LLMRateLimitError("the model provider is rate limiting us") from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise LLMUnavailableError("could not reach the model provider") from exc
        except openai.APIStatusError as exc:
            # 401/403 is an operator problem (bad or missing key). Any other
            # 4xx (400, 404, 422, …) means we sent a request the model will
            # never accept. Neither is transient: retrying resends the
            # identical bad request. Only a 5xx is actually "try again
            # later". This mirrors the Anthropic provider's mapping exactly
            # so the two providers behave identically to callers.
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

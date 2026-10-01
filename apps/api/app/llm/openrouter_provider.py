"""OpenRouter, an aggregator that fronts many vendors' models behind one API.

It serves OpenAI's Chat Completions wire format verbatim -- including
`stream=true` and `stream_options.include_usage` -- so everything that makes
`OpenAIProvider` correct (the usage-only final chunk with empty `choices`, the
empty-response guard, the 4xx/5xx mapping onto `LLMConfigurationError` vs
`LLMUnavailableError`) is correct here too, and is inherited rather than
copied.

What differs is the endpoint, the name, and model fallback (`_extra_body`
and `_stream` below): free ids are retired without notice, and an agent saved
with one must not go silent. The name matters beyond cosmetics:
it is persisted to `agents.provider` and reported on every `usage_events` row,
and it is what `app/llm/registry.py` resolves an agent's key from.
"""

from collections.abc import AsyncIterator, Awaitable, Callable

from app.core.logging import get_logger
from app.llm.errors import LLMModelRejectedError
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_models import get_free_models
from app.llm.types import CompletionRequest, StreamEvent

logger = get_logger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


# Most of OpenRouter's free roster are reasoning models, and OpenRouter returns
# their thinking on a `reasoning` field that the OpenAI-compatible `content`
# never carries. Left alone they spend the entire output budget thinking and
# stream an EMPTY reply that stops at `length` -- verified against the live API
# on 2026-09-16, where `nvidia/nemotron-3.5-lightning:free` returned 200 tokens
# of visible thinking and no answer, then answered in 29 tokens with this sent.
#
# The one endpoint this is invalid for is a model declaring
# `reasoning.mandatory`, which replies `400 Reasoning is mandatory for this
# endpoint and cannot be disabled`. `app/llm/openrouter_models.py` filters
# those out of everything this app offers, so the flag is safe to send
# unconditionally here.
_DISABLE_REASONING: dict[str, object] = {"reasoning": {"enabled": False}}


# How many other models ride along in OpenRouter's own `models` routing list.
# Kept short: every entry is a model that may answer instead of the one the
# owner picked, and a long list only hides a dead primary for longer.
_ROUTING_FALLBACKS = 2


async def _free_model_ids() -> list[str]:
    return [option.id for option in await get_free_models()]


class OpenRouterProvider(OpenAIProvider):
    def __init__(
        self,
        api_key: str,
        fallback_models: Callable[[], Awaitable[list[str]]] = _free_model_ids,
    ) -> None:
        super().__init__(api_key=api_key, base_url=OPENROUTER_BASE_URL, name="openrouter")
        # Injected so tests never reach OpenRouter's `/models`. The default is
        # the same cached, reasoning-filtered roster the dashboard offers, so
        # a fallback is never a model this app would refuse to offer.
        self._fallback_models = fallback_models

    async def _others(self, model: str) -> list[str]:
        return [other for other in await self._fallback_models() if other != model]

    async def _extra_body(self, request: CompletionRequest) -> dict[str, object] | None:
        # `models` is OpenRouter's own fallback routing: if the primary is
        # down, rate limited or refuses, OpenRouter tries the next without a
        # second round trip from us. It is not relied on for a primary id
        # that no longer exists, which may 400 the whole request before any
        # routing happens -- `_stream` below covers that case itself.
        fallbacks = (await self._others(request.model))[:_ROUTING_FALLBACKS]
        return {**_DISABLE_REASONING, "models": [request.model, *fallbacks]}

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        """The inherited stream, retried ONCE on another free model when
        OpenRouter rejects the model itself (`LLMModelRejectedError`: a
        retired id, or no endpoint serving it).

        Only a failure to produce the FIRST event is retried: the base class
        yields `message_start` only once the request is accepted, and after
        that a second model's answer would be spliced onto the first's. Only
        once: a 400 caused by the request rather than the model fails on every
        model, and each retry is another full round trip the visitor waits
        through. The retry's events carry the fallback's id, so the message
        and the `usage_events` row record what actually answered.
        """
        stream = super()._stream(request)
        try:
            first = await anext(stream)
        except LLMModelRejectedError as exc:
            candidates = await self._others(request.model)
            if not candidates:
                raise
            logger.warning(
                "llm_model_fallback",
                model=request.model,
                fallback_model=candidates[0],
                error=str(exc),
                body=getattr(exc.__cause__, "body", None),
            )
            stream = super()._stream(request.model_copy(update={"model": candidates[0]}))
            first = await anext(stream)
        yield first
        async for event in stream:
            yield event

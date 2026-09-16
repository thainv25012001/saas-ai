"""OpenRouter, an aggregator that fronts many vendors' models behind one API.

It serves OpenAI's Chat Completions wire format verbatim -- including
`stream=true` and `stream_options.include_usage` -- so everything that makes
`OpenAIProvider` correct (the usage-only final chunk with empty `choices`, the
empty-response guard, the 4xx/5xx mapping onto `LLMConfigurationError` vs
`LLMUnavailableError`) is correct here too, and is inherited rather than
copied.

What differs is the endpoint and the name. The name matters beyond cosmetics:
it is persisted to `agents.provider` and reported on every `usage_events` row,
and it is what `app/llm/registry.py` resolves an agent's key from.
"""

from app.llm.openai_provider import OpenAIProvider

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


class OpenRouterProvider(OpenAIProvider):
    def __init__(self, api_key: str) -> None:
        super().__init__(api_key=api_key, base_url=OPENROUTER_BASE_URL, name="openrouter")

    def _extra_body(self) -> dict[str, object] | None:
        return _DISABLE_REASONING

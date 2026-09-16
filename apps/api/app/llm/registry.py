from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.base import LLMProvider
from app.llm.errors import LLMConfigurationError
from app.llm.fake_provider import FakeProvider

_PROVIDERS: dict[str, LLMProvider] = {}

# Public, because it is the single source of truth for "is this a provider
# name we recognise" -- `app.agents.schemas` validates against it so an
# unknown string is rejected at the edge (as `invalid_input`) instead of
# reaching `create_agent` and being silently paired with an OpenAI model.
KNOWN_PROVIDERS = ("fake", "openai", "anthropic", "openrouter")

# Per PHASE-2.md §2.4. Used to resolve an agent's model when the caller
# names a provider but not a model: each provider's own idiomatic default,
# not a single hardcoded literal that would otherwise pair (e.g.) the
# `fake` provider with an OpenAI model string.
DEFAULT_MODELS: dict[str, str] = {
    "fake": "fake-1",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-opus-5",
    # OpenRouter's reason for existing here is its free tier, so the default
    # must be a `:free` id -- an agent created without an explicit model must
    # not start spending.
    #
    # Chosen by running this exact adapter against the live API on 2026-09-16,
    # not from the model list alone, because many free ids are REASONING
    # models: OpenRouter returns their thinking on a `reasoning` field the
    # OpenAI-compatible `content` never carries, so they answer a one-sentence
    # question with an empty string, `finish_reason="length"` and the entire
    # output budget spent (`z-ai/glm-5.2:free` took 80s to return nothing).
    # This one replies in ~1.3s with plain prose. Free ids are also retired
    # without notice, so treat it as a value to re-check, not assume.
    "openrouter": "google/gemma-4-31b-it:free",
}


def get_provider(name: str) -> LLMProvider:
    """Resolve a provider by name, constructing it at most once.

    Cached because each real provider holds an SDK client with its own connection
    pool; building one per request would leak sockets under load.
    """
    if name not in KNOWN_PROVIDERS:
        raise ValidationError(
            f"unknown provider '{name}'; expected one of {', '.join(KNOWN_PROVIDERS)}"
        )

    cached = _PROVIDERS.get(name)
    if cached is not None:
        return cached

    provider = _build(name)
    _PROVIDERS[name] = provider
    return provider


def _build(name: str) -> LLMProvider:
    settings = get_settings()
    if name == "fake":
        return FakeProvider()

    if name == "openai":
        if not settings.openai_api_key:
            raise LLMConfigurationError(
                "OPENAI_API_KEY is not set; set it or use the 'fake' provider"
            )
        # Task 3 adds this module. Imported lazily so `app.llm.registry` — and
        # the fake-provider path everything in this phase runs against — keeps
        # importing cleanly before it exists, and so a missing optional SDK at
        # runtime never breaks the fake path.
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key)

    if name == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMConfigurationError(
                "OPENROUTER_API_KEY is not set; set it or use the 'fake' provider"
            )
        # Lazy for the same reason as the branches above.
        from app.llm.openrouter_provider import OpenRouterProvider

        return OpenRouterProvider(api_key=settings.openrouter_api_key)

    if not settings.anthropic_api_key:
        raise LLMConfigurationError(
            "ANTHROPIC_API_KEY is not set; set it or use the 'fake' provider"
        )
    # Task 4 adds this module; see the OpenAI branch above for why the import
    # is lazy.
    from app.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key=settings.anthropic_api_key)


def reset_providers() -> None:
    """Drop cached providers. Tests call this after changing settings."""
    _PROVIDERS.clear()

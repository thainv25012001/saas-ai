from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.base import LLMProvider
from app.llm.errors import LLMConfigurationError
from app.llm.fake_provider import FakeProvider

_PROVIDERS: dict[str, LLMProvider] = {}
_KNOWN = ("fake", "openai", "anthropic")

# Per PHASE-2.md §2.4. Used to resolve an agent's model when the caller
# names a provider but not a model: each provider's own idiomatic default,
# not a single hardcoded literal that would otherwise pair (e.g.) the
# `fake` provider with an OpenAI model string.
DEFAULT_MODELS: dict[str, str] = {
    "fake": "fake-1",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-opus-5",
}


def get_provider(name: str) -> LLMProvider:
    """Resolve a provider by name, constructing it at most once.

    Cached because each real provider holds an SDK client with its own connection
    pool; building one per request would leak sockets under load.
    """
    if name not in _KNOWN:
        raise ValidationError(f"unknown provider '{name}'; expected one of {', '.join(_KNOWN)}")

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
        from app.llm.openai_provider import OpenAIProvider  # type: ignore[import-not-found]

        return OpenAIProvider(api_key=settings.openai_api_key)  # type: ignore[no-any-return]

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

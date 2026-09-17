from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.base import LLMProvider
from app.llm.errors import LLMConfigurationError
from app.llm.fake_provider import FakeProvider

# Eager, unlike the provider imports in `_build`: this module holds no SDK, only
# the OpenRouter model list and the ids that go with it.
from app.llm.openrouter_models import FALLBACK_MODELS

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
    # not start spending. Taken from `FALLBACK_MODELS` rather than repeated as
    # a literal: both are "an id verified to answer", both are retired without
    # notice, and two copies means retiring one leaves the other pointing at a
    # dead endpoint. That module documents how the list was verified.
    "openrouter": FALLBACK_MODELS[0].id,
}


# Which `Settings` field holds each provider's API key. `fake` is absent
# because it needs none. Defined once, and read by both `provider_is_configured`
# and `_build` below: the dashboard greys out a provider on exactly the
# condition that makes `_build` raise `LLMConfigurationError`, and two copies
# of that mapping would let the two answers drift apart.
_API_KEY_FIELDS: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "openrouter": "openrouter_api_key",
}


def provider_is_configured(name: str) -> bool:
    """Whether this provider has the API key it needs to answer.

    Public because the dashboard asks it before offering a provider: picking
    one with no key produces an agent that cannot answer, and the failure
    surfaces at chat time rather than at the dropdown where it was caused.
    """
    require_known_provider(name)
    field = _API_KEY_FIELDS.get(name)
    if field is None:
        return True  # `fake` answers offline; there is no key to be missing.
    return bool(getattr(get_settings(), field))


def require_known_provider(name: str) -> None:
    """Reject a provider name nothing here can serve.

    Public because `app/llm/catalog.py` asks the same question of the same
    tuple: one wording of the error -- which the integration tests assert on
    literally -- and one place to change when a provider is added.
    """
    if name not in KNOWN_PROVIDERS:
        raise ValidationError(
            f"unknown provider '{name}'; expected one of {', '.join(KNOWN_PROVIDERS)}"
        )


def get_provider(name: str) -> LLMProvider:
    """Resolve a provider by name, constructing it at most once.

    Cached because each real provider holds an SDK client with its own connection
    pool; building one per request would leak sockets under load.
    """
    require_known_provider(name)

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

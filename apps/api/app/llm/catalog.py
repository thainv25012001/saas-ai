"""What models the dashboard may offer for a given provider.

One entry point (`models_for`) so the UI asks the same question of every
provider, even though only OpenRouter publishes a model list: the rest are
short, hand-maintained sets that mirror `app/llm/pricing.py`, since a model
this app cannot cost is one it should not be suggesting.
"""

from collections.abc import Awaitable, Callable

from app.llm.openrouter_models import ModelOption, get_free_models
from app.llm.registry import require_known_provider

# Mirrors `MODEL_PRICING`, and `test_pricing.py` pins that it stays a subset of
# it. These providers have no models API worth calling -- OpenAI's `/models`
# lists embeddings, moderation and legacy snapshots with no field marking which
# are chat models, so it is noise, not a catalog.
_STATIC_MODELS: dict[str, tuple[ModelOption, ...]] = {
    "openai": (
        ModelOption("gpt-4o-mini", "GPT-4o mini", 128_000),
        ModelOption("gpt-4o", "GPT-4o", 128_000),
    ),
    "anthropic": (
        ModelOption("claude-haiku-4-5", "Claude Haiku 4.5", 200_000),
        ModelOption("claude-sonnet-5", "Claude Sonnet 5", 200_000),
        ModelOption("claude-opus-5", "Claude Opus 5", 200_000),
    ),
    "fake": (ModelOption("fake-1", "Fake (offline)", 8_000),),
}

# Providers that publish a roster worth fetching at request time. A lookup
# rather than a branch on the provider name: the next aggregator is a row here,
# not another `if` inside the generic entry point.
_LIVE_CATALOGS: dict[str, Callable[[], Awaitable[list[ModelOption]]]] = {
    "openrouter": get_free_models,
}


async def models_for(provider: str) -> list[ModelOption]:
    require_known_provider(provider)
    fetch = _LIVE_CATALOGS.get(provider)
    if fetch is not None:
        return await fetch()
    # Indexed rather than `.get(provider, ())`: a known provider with no models
    # listed here is a mistake, and it should fail loudly in a test instead of
    # reaching the dashboard as an empty dropdown nobody can explain.
    return list(_STATIC_MODELS[provider])

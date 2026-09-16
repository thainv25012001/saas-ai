"""What models the dashboard may offer for a given provider.

One entry point (`models_for`) so the UI asks the same question of every
provider, even though only OpenRouter publishes a model list: the rest are
short, hand-maintained sets that mirror `app/llm/pricing.py`, since a model
this app cannot cost is one it should not be suggesting.
"""

from app.core.errors import ValidationError
from app.llm.openrouter_models import ModelOption, get_free_models
from app.llm.registry import KNOWN_PROVIDERS

# Mirrors `MODEL_PRICING`. These providers have no models API worth calling --
# OpenAI's `/models` lists embeddings, moderation and legacy snapshots with no
# field marking which are chat models, so it is noise, not a catalog.
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


async def models_for(provider: str) -> list[ModelOption]:
    if provider not in KNOWN_PROVIDERS:
        raise ValidationError(
            f"unknown provider '{provider}'; expected one of {', '.join(KNOWN_PROVIDERS)}"
        )
    if provider == "openrouter":
        return await get_free_models()
    return list(_STATIC_MODELS.get(provider, ()))

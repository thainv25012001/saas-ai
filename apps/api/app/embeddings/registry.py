from app.core.config import get_settings
from app.core.errors import ValidationError
from app.embeddings.base import EmbeddingProvider
from app.embeddings.hashing import HashingEmbedder
from app.llm.errors import LLMConfigurationError

_PROVIDERS: dict[str, EmbeddingProvider] = {}

# Public for the same reason as `app.llm.registry.KNOWN_PROVIDERS`: the single
# source of truth for "is this a name we recognise", so an unknown string is
# rejected here rather than reaching an ingest job.
KNOWN_EMBEDDING_PROVIDERS = ("hashing", "openai")


def get_embedding_provider(name: str | None = None) -> EmbeddingProvider:
    """Resolve an embedding provider by name, constructing it at most once.

    `name=None` resolves to `settings.embedding_provider` so ingestion and
    retrieval can both call this with no argument and stay pointed at
    whichever embedder the deployment is configured for. Cached because the
    OpenAI provider holds an SDK client with its own connection pool;
    building one per chunk would leak sockets under load.
    """
    resolved = name if name is not None else get_settings().embedding_provider
    if resolved not in KNOWN_EMBEDDING_PROVIDERS:
        raise ValidationError(
            f"unknown embedding provider '{resolved}'; expected one of "
            f"{', '.join(KNOWN_EMBEDDING_PROVIDERS)}"
        )

    cached = _PROVIDERS.get(resolved)
    if cached is not None:
        return cached

    provider = _build(resolved)
    _PROVIDERS[resolved] = provider
    return provider


def _build(name: str) -> EmbeddingProvider:
    if name == "hashing":
        return HashingEmbedder()

    settings = get_settings()
    if not settings.openai_api_key:
        raise LLMConfigurationError(
            "OPENAI_API_KEY is not set; set it or use the 'hashing' provider"
        )
    # Imported lazily so this module -- and the hashing path everything in
    # this phase runs against with no key configured -- keeps importing
    # cleanly before the OpenAI SDK is needed.
    from app.embeddings.openai_embedder import OpenAIEmbeddingProvider

    return OpenAIEmbeddingProvider(api_key=settings.openai_api_key)


def reset_embedding_providers() -> None:
    """Drop cached providers. Tests call this after changing settings."""
    _PROVIDERS.clear()

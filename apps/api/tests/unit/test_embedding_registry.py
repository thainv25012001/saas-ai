import pytest

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.embeddings.registry import get_embedding_provider, reset_embedding_providers


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_embedding_providers()
    get_settings.cache_clear()
    yield
    reset_embedding_providers()
    get_settings.cache_clear()


def test_hashing_resolves_without_any_api_key():
    assert get_embedding_provider("hashing").name == "hashing"


def test_no_name_resolves_to_the_configured_default():
    """`settings.embedding_provider` defaults to `hashing`, so calling with no
    argument must behave exactly like asking for it explicitly."""
    assert get_embedding_provider().name == get_settings().embedding_provider


def test_unknown_provider_name_is_a_validation_error():
    with pytest.raises(ValidationError):
        get_embedding_provider("definitely-not-a-provider")


def test_providers_are_cached_per_name():
    """Constructing an SDK client per chunk would leak connection pools."""
    assert get_embedding_provider("hashing") is get_embedding_provider("hashing")


def test_openai_without_a_key_is_a_configuration_error(monkeypatch):
    """Patch what the registry actually reads, not the ambient environment: a
    test whose result depends on whether the developer happens to have a real
    OPENAI_API_KEY in .env is not a test."""
    from app.llm.errors import LLMConfigurationError

    base = get_settings()
    no_key_settings = base.model_copy(update={"openai_api_key": None})
    monkeypatch.setattr("app.embeddings.registry.get_settings", lambda: no_key_settings)
    reset_embedding_providers()
    with pytest.raises(LLMConfigurationError):
        get_embedding_provider("openai")


def test_openai_with_a_key_resolves(monkeypatch):
    base = get_settings()
    keyed = base.model_copy(update={"openai_api_key": "test-key"})
    monkeypatch.setattr("app.embeddings.registry.get_settings", lambda: keyed)
    reset_embedding_providers()

    provider = get_embedding_provider("openai")

    assert provider.name == "openai"
    assert provider.dimensions == 1536

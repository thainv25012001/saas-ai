import pytest

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.registry import get_provider, reset_providers


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_providers()
    get_settings.cache_clear()
    yield
    reset_providers()
    get_settings.cache_clear()


def test_fake_provider_resolves_without_any_api_key():
    assert get_provider("fake").name == "fake"


def test_unknown_provider_name_is_a_validation_error():
    with pytest.raises(ValidationError):
        get_provider("definitely-not-a-provider")


def test_providers_are_cached_per_name():
    """Constructing an SDK client per request would leak connection pools."""
    assert get_provider("fake") is get_provider("fake")


def test_openai_without_a_key_is_a_configuration_error(monkeypatch):
    """Patch what the registry actually reads, not the ambient environment: a
    test whose result depends on whether the developer happens to have a real
    OPENAI_API_KEY in .env is not a test."""
    from app.llm.errors import LLMConfigurationError

    base = get_settings()
    no_key_settings = base.model_copy(update={"openai_api_key": None})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: no_key_settings)
    reset_providers()
    with pytest.raises(LLMConfigurationError):
        get_provider("openai")

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


def test_openrouter_without_a_key_is_a_configuration_error(monkeypatch):
    from app.llm.errors import LLMConfigurationError

    base = get_settings()
    no_key_settings = base.model_copy(update={"openrouter_api_key": None})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: no_key_settings)
    reset_providers()
    with pytest.raises(LLMConfigurationError):
        get_provider("openrouter")


def test_openrouter_resolves_to_its_own_provider_pointed_at_openrouter(monkeypatch):
    """OpenRouter speaks OpenAI's wire format, so it reuses that adapter -- but
    it must stay a distinct provider with its own name and base URL, or an
    agent configured for OpenRouter would silently bill an OpenAI key."""
    base = get_settings()
    keyed = base.model_copy(update={"openrouter_api_key": "test-key"})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: keyed)
    reset_providers()

    provider = get_provider("openrouter")

    assert provider.name == "openrouter"
    assert "openrouter.ai" in str(provider._client.base_url)  # noqa: SLF001


def test_the_openrouter_default_model_is_a_free_one():
    """The point of adding this provider was the free tier. A default that
    is not `:free` spends real money on an agent created with no model set."""
    from app.llm.registry import DEFAULT_MODELS

    assert DEFAULT_MODELS["openrouter"].endswith(":free")


def _settings_with(**overrides):
    """Settings the registry will actually read, with every provider key
    cleared first. Tests that assert on "is this key set" are worthless if
    the answer depends on the developer's own .env."""
    base = get_settings()
    return base.model_copy(
        update={
            "openai_api_key": None,
            "anthropic_api_key": None,
            "openrouter_api_key": None,
            **overrides,
        }
    )


def test_fake_is_always_configured():
    """It needs no key at all -- that is the entire point of it."""
    from app.llm.registry import provider_is_configured

    assert provider_is_configured("fake") is True


def test_a_provider_without_its_key_is_not_configured(monkeypatch):
    from app.llm.registry import provider_is_configured

    monkeypatch.setattr("app.llm.registry.get_settings", lambda: _settings_with())

    assert provider_is_configured("openai") is False
    assert provider_is_configured("anthropic") is False
    assert provider_is_configured("openrouter") is False


def test_a_provider_with_its_key_is_configured(monkeypatch):
    from app.llm.registry import provider_is_configured

    monkeypatch.setattr(
        "app.llm.registry.get_settings",
        lambda: _settings_with(anthropic_api_key="test-key"),
    )

    assert provider_is_configured("anthropic") is True
    # Only the one whose key is set -- a single key must not light up the rest.
    assert provider_is_configured("openai") is False


def test_configured_check_rejects_an_unknown_provider():
    from app.llm.registry import provider_is_configured

    with pytest.raises(ValidationError):
        provider_is_configured("definitely-not-a-provider")

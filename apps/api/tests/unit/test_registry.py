import pytest

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.registry import KNOWN_PROVIDERS, get_provider, reset_providers


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_providers()
    get_settings.cache_clear()
    yield
    reset_providers()
    get_settings.cache_clear()


def test_the_known_providers_are_the_three_real_ones():
    assert KNOWN_PROVIDERS == ("openai", "anthropic", "openrouter")


def test_fake_is_no_longer_a_provider():
    """It was an offline stand-in the app shipped; it now lives in the tests."""
    with pytest.raises(ValidationError):
        get_provider("fake")


def test_unknown_provider_name_is_a_validation_error():
    with pytest.raises(ValidationError):
        get_provider("definitely-not-a-provider")


def test_providers_are_cached_per_name(monkeypatch):
    """Constructing an SDK client per request would leak connection pools."""
    keyed = get_settings().model_copy(update={"openrouter_api_key": "test-key"})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: keyed)
    reset_providers()
    assert get_provider("openrouter") is get_provider("openrouter")


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
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_API_KEY is not set"):
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


def test_every_known_provider_has_a_default_model():
    from app.llm.registry import DEFAULT_MODELS

    assert set(DEFAULT_MODELS) == set(KNOWN_PROVIDERS)


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


def test_every_known_provider_needs_a_key(monkeypatch):
    """No provider answers offline any more, so none is configured by default."""
    from app.llm.registry import provider_is_configured

    monkeypatch.setattr("app.llm.registry.get_settings", lambda: _settings_with())

    assert {name: provider_is_configured(name) for name in KNOWN_PROVIDERS} == {
        "openai": False,
        "anthropic": False,
        "openrouter": False,
    }


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

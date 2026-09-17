"""`CreateAgentInput` requires a provider and a model.

Creating an agent used to take a name alone and inherit `DEFAULT_LLM_PROVIDER`
— `fake` out of the box — so every agent a real user made through the
dashboard silently started on the offline provider that answers with a canned
reply. The choice is the caller's now, and there is no default to fall back to.
"""

import pytest
from pydantic import ValidationError

from app.agents.schemas import CreateAgentInput, UpdateAgentInput


def test_create_requires_a_provider():
    with pytest.raises(ValidationError) as exc:
        CreateAgentInput(name="Sales Bot", model="gpt-4o-mini")  # type: ignore[call-arg]
    assert exc.value.error_count() == 1
    assert exc.value.errors()[0]["loc"] == ("provider",)


def test_create_requires_a_model():
    with pytest.raises(ValidationError) as exc:
        CreateAgentInput(name="Sales Bot", provider="openai")  # type: ignore[call-arg]
    assert exc.value.error_count() == 1
    assert exc.value.errors()[0]["loc"] == ("model",)


def test_create_rejects_a_null_provider():
    """Distinct from omitting it: the GraphQL layer can pass an explicit null,
    and "no preference" must not be a way back to the old fallback."""
    with pytest.raises(ValidationError):
        CreateAgentInput(name="Sales Bot", provider=None, model="gpt-4o-mini")  # type: ignore[arg-type]


def test_create_rejects_an_empty_model():
    """A blank string is not a choice. The dashboard's model picker starts
    empty, so this is what an unfilled form sends."""
    with pytest.raises(ValidationError) as exc:
        CreateAgentInput(name="Sales Bot", provider="openai", model="")
    assert exc.value.errors()[0]["loc"] == ("model",)


def test_create_rejects_an_unknown_provider():
    with pytest.raises(ValidationError) as exc:
        CreateAgentInput(name="Sales Bot", provider="gpt", model="gpt-4o-mini")
    assert "fake" in str(exc.value)


def test_create_accepts_a_known_provider_and_model():
    parsed = CreateAgentInput(name="Sales Bot", provider="anthropic", model="claude-opus-5")
    assert (parsed.provider, parsed.model) == ("anthropic", "claude-opus-5")


def test_update_still_treats_both_as_optional():
    """`UpdateAgentInput` is a partial update: `None` means "leave it alone",
    not "pick one for me". Making create strict must not make it impossible to
    rename an agent without restating its model."""
    parsed = UpdateAgentInput(name="Renamed")
    assert parsed.provider is None
    assert parsed.model is None

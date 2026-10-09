"""Input builders shared by the test suite.

`CreateAgentInput` requires `provider` and `model` — the API deliberately has
no default, so that a user creating an agent through the dashboard has to
choose one rather than silently landing on whatever `DEFAULT_LLM_PROVIDER`
happens to be. That is right for the product and tedious for tests, almost
none of which care which provider an agent is on.

This fills them in with `openrouter`, which `tests/conftest.py` serves from the
`FakeProvider` test double. A test that *does* care passes its own.
"""

from app.agents.schemas import CreateAgentInput

# A fixed pair, not `DEFAULT_MODELS["openrouter"]`: a test fixture that follows
# the registry would change silently with it. The `:free` suffix prices it at
# zero, so cost assertions see 0 rather than NULL.
TEST_PROVIDER = "openrouter"
TEST_MODEL = "test/model:free"


def agent_input(name: str = "Sales Bot", **overrides: object) -> CreateAgentInput:
    """A valid `CreateAgentInput`, on the test provider unless told otherwise."""
    fields: dict[str, object] = {
        "provider": TEST_PROVIDER,
        "model": TEST_MODEL,
        **overrides,
    }
    return CreateAgentInput(name=name, **fields)  # type: ignore[arg-type]

"""Input builders shared by the test suite.

`CreateAgentInput` requires `provider` and `model` — the API deliberately has
no default, so that a user creating an agent through the dashboard has to
choose one rather than silently landing on whatever `DEFAULT_LLM_PROVIDER`
happens to be. That is right for the product and tedious for tests, almost
none of which care which provider an agent is on.

This fills them in with the offline provider, which needs no API key and is
why `fake` still exists at all. A test that *does* care passes its own.
"""

from app.agents.schemas import CreateAgentInput

# The offline pair. Not `DEFAULT_MODELS["fake"]`: a test fixture that follows
# the registry would change silently with it, and these tests want a fixed
# starting point they can assert against.
FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake-1"


def agent_input(name: str = "Sales Bot", **overrides: object) -> CreateAgentInput:
    """A valid `CreateAgentInput`, on the offline provider unless told otherwise."""
    fields: dict[str, object] = {
        "provider": FAKE_PROVIDER,
        "model": FAKE_MODEL,
        **overrides,
    }
    return CreateAgentInput(name=name, **fields)  # type: ignore[arg-type]

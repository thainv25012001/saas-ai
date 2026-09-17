"""The dev seed is the one caller that legitimately wants the default provider.

`CreateAgentInput` requires a provider and a model now, so the seed has to name
them. It is also the only remaining consumer of `DEFAULT_LLM_PROVIDER` — a
fresh clone has no API key at all, so the demo agent has to be able to answer
offline.

Unit, not integration: this asserts on how the seed builds its input, without
needing a database it would then have to clean up.
"""

from app.agents.schemas import CreateAgentInput
from app.core.config import get_settings
from app.db.seed import demo_agent_input
from app.llm.registry import DEFAULT_MODELS


def test_demo_agent_input_is_valid():
    """The regression this file exists for: the seed used to pass a name alone,
    which stopped parsing the moment provider and model became required."""
    assert isinstance(demo_agent_input(), CreateAgentInput)


def test_demo_agent_uses_the_configured_default_provider():
    parsed = demo_agent_input()
    assert parsed.provider == get_settings().default_llm_provider


def test_demo_agent_model_matches_its_provider():
    """Never a hardcoded literal: pairing the `fake` provider with an OpenAI
    model id would give the demo an agent that cannot answer."""
    parsed = demo_agent_input()
    assert parsed.model == DEFAULT_MODELS[parsed.provider]

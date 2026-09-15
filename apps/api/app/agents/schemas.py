from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.llm.registry import KNOWN_PROVIDERS


def _known_provider(value: str | None) -> str | None:
    """Reject a provider name no adapter exists for.

    Validated here, in the schema shared by create and update, rather than in
    `AgentService`: `create_agent` would otherwise fall back to
    `DEFAULT_MODELS["openai"]` for an unrecognised name (silently pairing a
    typo'd provider with an OpenAI model), and `update_agent` `setattr`s the
    column with no check at all. A `ValueError` raised here is turned into
    the app's own `invalid_input` error by `app.graphql.resolvers._build`
    (and by FastAPI's `RequestValidationError` handler on the REST side), so
    both mutations reject the same values the same way.

    `None` is not a rejection: it means "caller expressed no preference" and
    is resolved from settings/left unchanged downstream.
    """
    if value is not None and value not in KNOWN_PROVIDERS:
        raise ValueError(f"must be one of: {', '.join(KNOWN_PROVIDERS)}")
    return value


Provider = Annotated[str | None, AfterValidator(_known_provider)]


class CreateAgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # None means "caller expressed no preference" and is resolved by
    # AgentService.create_agent to Settings.default_llm_provider — "fake"
    # until a real API key is configured, so a fresh clone gets a working
    # agent with no key required.
    provider: Provider = None
    # None means "caller expressed no preference" and is resolved by
    # AgentService.create_agent to the *chosen* provider's own default model
    # (app.llm.registry.DEFAULT_MODELS) — never a hardcoded OpenAI model
    # string, which would be incoherent when the provider resolves to
    # something else (e.g. `fake`).
    model: str | None = None
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=32_000)


class UpdateAgentInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = None
    provider: Provider = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32_000)


class UpdateAgentConfigInput(BaseModel):
    persona: str | None = None
    tone: str | None = None
    language: str | None = None
    greeting: str | None = None
    fallback_message: str | None = None
    enabled_tool_names: list[str] | None = None
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_agent_steps: int | None = Field(default=None, ge=1, le=20)

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.llm.registry import KNOWN_PROVIDERS


def _known_provider(value: str | None) -> str | None:
    """Reject a provider name no adapter exists for.

    Validated here, in the schema shared by create and update, rather than in
    `AgentService`: `update_agent` `setattr`s the column with no check at all,
    and `create_agent` trusts what it is handed. A `ValueError` raised here is
    turned into the app's own `invalid_input` error by
    `app.graphql.resolvers._build` (and by FastAPI's
    `RequestValidationError` handler on the REST side), so both mutations
    reject the same values the same way.

    `None` passes, because the *optional* alias below is what `UpdateAgentInput`
    uses, where it means "leave this field unchanged". On create the field is
    required, so pydantic rejects a missing or null value before this runs.
    """
    if value is not None and value not in KNOWN_PROVIDERS:
        raise ValueError(f"must be one of: {', '.join(KNOWN_PROVIDERS)}")
    return value


# Update is a partial: `None` means "unchanged".
Provider = Annotated[str | None, AfterValidator(_known_provider)]
# Create is not: there is no default provider to fall back to.
RequiredProvider = Annotated[str, AfterValidator(_known_provider)]


class CreateAgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # Required, with no default on either side of the wire. Both fields used to
    # be optional and were resolved downstream — `provider` from
    # `Settings.default_llm_provider` (`fake` out of the box) and `model` from
    # that provider's entry in `DEFAULT_MODELS`. The dashboard only ever sent a
    # name, so in practice every agent a real user created started on the
    # offline provider that answers with a canned reply, and they had to
    # discover that and fix it on the agent's detail page. Requiring the choice
    # here is what makes the create form ask for it.
    provider: RequiredProvider
    # `min_length=1` matters as much as requiredness: the dashboard's model
    # picker starts empty, so an unfilled form sends "" rather than omitting
    # the field.
    model: str = Field(min_length=1, max_length=100)
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
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_agent_steps: int | None = Field(default=None, ge=1, le=20)

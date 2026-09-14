from pydantic import BaseModel, Field


class CreateAgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    # None means "caller expressed no preference" and is resolved by
    # AgentService.create_agent to Settings.default_llm_provider — "fake"
    # until a real API key is configured, so a fresh clone gets a working
    # agent with no key required.
    provider: str | None = None
    model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=32_000)


class UpdateAgentInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = None
    provider: str | None = None
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

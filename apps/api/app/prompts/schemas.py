from pydantic import BaseModel, Field


class CreatePromptInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    description: str | None = None
    system_prompt: str = Field(min_length=1)


class CreateVersionInput(BaseModel):
    system_prompt: str = Field(min_length=1)
    notes: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)

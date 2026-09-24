"""Pydantic write shape for an API key's name. `ApiKeyService.create` takes
a plain `name: str` (docs/PHASE-7.md §6/§3, and `.superpowers/sdd/...
task-2-brief.md`'s interface) so nothing but a string needs to reach it --
but that means the service, not this schema, is what a raw caller (this
task's tests included) actually has to trust. `create` validates through
this model itself for exactly that reason: one place decides "1..100 chars,
stripped" today, and Task 4's GraphQL `createApiKey(agentId, name)` mutation
input reuses it unchanged rather than re-declaring the same constraint.
"""

from pydantic import BaseModel, field_validator


class CreateApiKeyInput(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _stripped_and_bounded(cls, value: str) -> str:
        stripped = value.strip()
        if not (1 <= len(stripped) <= 100):
            raise ValueError("name must be between 1 and 100 characters")
        return stripped

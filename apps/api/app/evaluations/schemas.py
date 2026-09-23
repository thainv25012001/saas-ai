"""Pydantic write shapes for `EvaluationService`: datasets and cases (Task
1) and starting a run (Task 4). `StartRunInput` is the body of the REST
`POST /api/v1/evaluations/runs`, not a GraphQL input -- see docs/PHASE-6.md
§7 for why starting a run is REST.
"""

import uuid
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.tools.leads import CreateLeadTool
from app.tools.products import GetProductTool, SearchProductsTool
from app.tools.retrieve import RetrieveKnowledgeTool

# The single source of truth for "which tool name can a case expect the
# model to call" -- each class's own `name`, the same attribute
# `app/chat/service.py::_BUILTIN_TOOL_CLASSES` constructs from and
# `ToolRegistry` keys on. Deliberately not a fresh literal list of strings,
# which could drift the moment a fifth builtin tool is added and this file
# is not updated to match.
KNOWN_BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    tool_cls.name
    for tool_cls in (RetrieveKnowledgeTool, CreateLeadTool, SearchProductsTool, GetProductTool)
)

MAX_CASES_PER_DATASET = 200

_MAX_REQUIRED_PHRASES = 20
_MAX_EXPECTED_IDS = 20
_MAX_TAGS = 10


def _dedupe(values: list[str]) -> list[str]:
    """First occurrence of each value, order preserved."""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _dedupe_uuids(values: list[uuid.UUID]) -> list[uuid.UUID]:
    seen: dict[uuid.UUID, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


class CreateDatasetInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class UpdateDatasetInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be blank")
        return stripped


class CaseInput(BaseModel):
    """The full write shape for both `create_case` and `update_case`
    (`update_case` is a full replace, not a patch -- see
    `EvaluationService.update_case`)."""

    question: str = Field(min_length=1, max_length=4000)
    reference_answer: str | None = Field(default=None, max_length=4000)
    required_phrases: list[str] = Field(default_factory=list)
    expected_tool_names: list[str] = Field(default_factory=list)
    expected_document_ids: list[uuid.UUID] = Field(default_factory=list)
    expected_product_ids: list[uuid.UUID] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped

    @field_validator("reference_answer")
    @classmethod
    def _strip_reference_answer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("required_phrases")
    @classmethod
    def _validate_required_phrases(cls, values: list[str]) -> list[str]:
        if len(values) > _MAX_REQUIRED_PHRASES:
            raise ValueError(f"at most {_MAX_REQUIRED_PHRASES} required phrases are allowed")
        stripped: list[str] = []
        for value in values:
            phrase = value.strip()
            if not 1 <= len(phrase) <= 200:
                raise ValueError("each required phrase must be 1-200 characters")
            stripped.append(phrase)
        return _dedupe(stripped)

    @field_validator("expected_tool_names")
    @classmethod
    def _validate_expected_tool_names(cls, values: list[str]) -> list[str]:
        unknown = sorted({value for value in values if value not in KNOWN_BUILTIN_TOOL_NAMES})
        if unknown:
            raise ValueError(f"unknown tool name(s): {', '.join(unknown)}")
        return values

    @field_validator("expected_document_ids")
    @classmethod
    def _validate_expected_document_ids(cls, values: list[uuid.UUID]) -> list[uuid.UUID]:
        # Checked before deduping, same as `required_phrases`: the cap
        # bounds the payload a caller may submit, not just what survives it --
        # otherwise a caller could send an unbounded list of repeats and rely
        # on dedup to always bring it back under the limit.
        if len(values) > _MAX_EXPECTED_IDS:
            raise ValueError(f"at most {_MAX_EXPECTED_IDS} expected_document_ids are allowed")
        return _dedupe_uuids(values)

    @field_validator("expected_product_ids")
    @classmethod
    def _validate_expected_product_ids(cls, values: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(values) > _MAX_EXPECTED_IDS:
            raise ValueError(f"at most {_MAX_EXPECTED_IDS} expected_product_ids are allowed")
        return _dedupe_uuids(values)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, values: list[str]) -> list[str]:
        for value in values:
            if not 1 <= len(value) <= 50:
                raise ValueError("each tag must be 1-50 characters")
        return values

    @model_validator(mode="after")
    def _at_least_one_expectation(self) -> Self:
        if not (
            self.reference_answer
            or self.required_phrases
            or self.expected_tool_names
            or self.expected_document_ids
            or self.expected_product_ids
        ):
            raise ValueError(
                "a case needs at least one expectation: reference_answer, "
                "required_phrases, expected_tool_names, expected_document_ids "
                "or expected_product_ids"
            )
        return self


class StartRunInput(BaseModel):
    """What `POST /api/v1/evaluations/runs` accepts (docs/PHASE-6.md §5).

    `provider`/`model` and `judge_provider`/`judge_model` are each "both or
    neither": a provider with no model has no sensible default to fall back
    to (the agent's own model belongs to the agent's own provider), and a
    model with no provider is ambiguous. Whether a named provider is known
    and has a key configured is checked by `EvaluationService.create_run`,
    not here, so the resolved pair (the agent's own, when neither is named)
    goes through exactly the same check.
    """

    dataset_id: uuid.UUID
    agent_id: uuid.UUID
    prompt_version_id: uuid.UUID | None = None
    provider: str | None = Field(default=None, min_length=1, max_length=50)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    judge_provider: str | None = Field(default=None, min_length=1, max_length=50)
    judge_model: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _pairs_are_both_or_neither(self) -> Self:
        if (self.provider is None) != (self.model is None):
            raise ValueError("provider and model must be given together, or neither")
        if (self.judge_provider is None) != (self.judge_model is None):
            raise ValueError("judge_provider and judge_model must be given together, or neither")
        return self

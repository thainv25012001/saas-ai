"""Structured output for providers whose wire format only returns text.

A provider answers `generate_structured` by delegating here. The caller's
system prompt gets one more instruction (reply with a single JSON object that
conforms to the schema, which is appended), the reply comes back through the
provider's ordinary `generate()`, and Pydantic decides whether it is valid.

Not `response_format` or a forced tool call: OpenRouter's free roster changes
without notice and support for either varies by model, while every model can
be asked for JSON. `app/evaluations/judge.py` reached the same conclusion first.

Anything but one conforming object raises `LLMStructuredOutputError`. Callers
decide what a failure means; the supplier pipeline turns it into a review.
"""

import json

from pydantic import ValidationError

from app.core.logging import get_logger
from app.llm.base import LLMProvider, SchemaT
from app.llm.errors import LLMStructuredOutputError
from app.llm.types import CompletionRequest

logger = get_logger(__name__)

_INSTRUCTION = (
    "Reply with exactly one JSON object that conforms to the JSON Schema below. "
    "Do not add any other text and do not wrap the object in a code fence.\n\n"
    "JSON Schema:\n"
)


def strip_json_fence(text: str) -> str:
    """Undo a ```json ... ``` (or bare ``` ... ```) wrapper some models add
    despite being told not to. Text that does not start with a fence is
    returned trimmed but otherwise unchanged; validation is left to reject
    anything that still is not valid JSON."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines:
        lines = lines[1:]  # drop the opening ``` or ```json line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


async def generate_structured(
    provider: LLMProvider, request: CompletionRequest, schema: type[SchemaT]
) -> SchemaT:
    instruction = _INSTRUCTION + json.dumps(schema.model_json_schema())
    system = f"{request.system}\n\n{instruction}" if request.system else instruction
    response = await provider.generate(request.model_copy(update={"system": system}))
    text = strip_json_fence(response.text)
    if not text:
        _log_failure(request.model, "EmptyReply")
        raise LLMStructuredOutputError("the model returned an empty reply")
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        # Pydantic reports invalid JSON as a ValidationError too, so this one
        # clause covers prose, arrays, `null` and wrong or missing fields.
        _log_failure(request.model, type(exc).__name__)
        # `from None`: the ValidationError embeds the reply (`input_value`).
        raise LLMStructuredOutputError(
            "the model reply does not match the expected schema"
        ) from None


def _log_failure(model: str, error_type: str) -> None:
    # Never the reply itself: it can quote supplier prices or customer text.
    logger.warning("llm_structured_output_invalid", model=model, error_type=error_type)

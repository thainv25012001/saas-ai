"""`generate_structured` turns a provider's plain-text reply into a validated
schema instance, and refuses anything that is not exactly that."""

from typing import Literal

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from app.llm.errors import LLMStructuredOutputError
from app.llm.structured import generate_structured, strip_json_fence
from app.llm.types import CompletionRequest, CompletionResponse, Message, TextBlock, Usage

pytestmark = pytest.mark.anyio


class _Verdict(BaseModel):
    label: Literal["supplier", "other"]
    confidence: float


class _ReplyingProvider:
    """Answers every `generate()` with one fixed text and keeps the request."""

    name = "openrouter"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.request: CompletionRequest | None = None

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        self.request = request
        return CompletionResponse(
            content=[TextBlock(text=self.reply)], usage=Usage(), model=request.model
        )


def _request(system: str = "Classify the sender.") -> CompletionRequest:
    return CompletionRequest(
        model="test/model:free",
        messages=[Message.text("user", "quotes@acme.example")],
        system=system,
        max_tokens=200,
    )


async def test_a_plain_json_reply_is_validated_into_the_schema():
    provider = _ReplyingProvider('{"label": "supplier", "confidence": 0.9}')
    result = await generate_structured(provider, _request(), _Verdict)
    assert result == _Verdict(label="supplier", confidence=0.9)


async def test_a_fenced_json_reply_is_unwrapped():
    provider = _ReplyingProvider('```json\n{"label": "other", "confidence": 0.1}\n```')
    result = await generate_structured(provider, _request(), _Verdict)
    assert result == _Verdict(label="other", confidence=0.1)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "I think this is a supplier.",
        'Sure! Here it is: {"label": "supplier", "confidence": 0.9}',
        '{"label": "supplier"}',
        '{"label": "vendor", "confidence": 0.9}',
        '[{"label": "supplier", "confidence": 0.9}]',
        "null",
    ],
)
async def test_anything_but_one_conforming_object_is_refused(reply):
    provider = _ReplyingProvider(reply)
    with pytest.raises(LLMStructuredOutputError):
        await generate_structured(provider, _request(), _Verdict)


async def test_the_callers_system_prompt_is_kept_and_the_schema_appended():
    provider = _ReplyingProvider('{"label": "other", "confidence": 0.0}')
    await generate_structured(provider, _request("Classify the sender."), _Verdict)
    assert provider.request is not None
    system = provider.request.system
    assert system.startswith("Classify the sender.")
    assert "JSON Schema" in system
    assert '"confidence"' in system
    # Only the system prompt changes: the untrusted data stays in the user turn.
    assert provider.request.messages == _request().messages
    assert provider.request.model == "test/model:free"


async def test_an_empty_system_prompt_gets_only_the_instruction():
    provider = _ReplyingProvider('{"label": "other", "confidence": 0.0}')
    await generate_structured(provider, _request(system=""), _Verdict)
    assert provider.request is not None
    assert provider.request.system.startswith("Reply with exactly one JSON object")


async def test_a_failure_is_logged_without_the_reply_text():
    """Replies carry supplier prices; a log line is not the place for them."""
    provider = _ReplyingProvider("ACME quotes 12.50 per unit")
    with capture_logs() as logs, pytest.raises(LLMStructuredOutputError):
        await generate_structured(provider, _request(), _Verdict)
    assert [entry["event"] for entry in logs] == ["llm_structured_output_invalid"]
    assert logs[0]["model"] == "test/model:free"
    assert logs[0]["error_type"] == "ValidationError"
    assert "12.50" not in str(logs)


async def test_a_refusal_does_not_chain_the_validation_error():
    """The chained ValidationError embeds the reply text (`input_value`)."""
    provider = _ReplyingProvider("ACME quotes 12.50 per unit")
    with pytest.raises(LLMStructuredOutputError) as raised:
        await generate_structured(provider, _request(), _Verdict)
    assert raised.value.__cause__ is None


def test_text_without_a_fence_is_only_trimmed():
    assert strip_json_fence('  {"a": 1}\n') == '{"a": 1}'

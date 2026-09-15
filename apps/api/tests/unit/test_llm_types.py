import pytest
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import AppError
from app.llm.errors import (
    LLMConfigurationError,
    LLMEmptyResponseError,
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.types import (
    CompletionRequest,
    ErrorEvent,
    Message,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
    Usage,
)


def test_message_text_helper_builds_a_single_text_block():
    message = Message.text("user", "hello")
    assert message.role == "user"
    assert len(message.content) == 1
    assert message.content[0].type == "text"


def test_text_content_joins_only_text_blocks():
    """Tool blocks must not leak into the text rendering — Phase 4 relies on this."""
    message = Message(
        role="assistant",
        content=[
            TextBlock(text="Let me check. "),
            ToolUseBlock(id="t1", name="search", input={"q": "x"}),
            TextBlock(text="Found it."),
        ],
    )
    assert message.text_content == "Let me check. Found it."


def test_content_blocks_discriminate_on_type():
    """Round-trips raw dicts (not Python objects) through validation, the only path
    where pydantic's discriminator actually does anything. A regression here would
    let a `tool_use` payload silently parse as the wrong block class."""
    message = Message.model_validate(
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t", "name": "n", "input": {}},
                {"type": "text", "text": "x"},
            ],
        }
    )
    assert isinstance(message.content[0], ToolUseBlock)
    assert isinstance(message.content[1], TextBlock)


def test_stream_events_discriminate_on_type():
    """Same guard as above, for the StreamEvent union used by the SSE layer."""
    event = TypeAdapter(StreamEvent).validate_python({"type": "error", "code": "c", "message": "m"})
    assert isinstance(event, ErrorEvent)


def test_completion_request_defaults_temperature_to_none():
    """None means 'the agent did not ask for a specific value', which is different
    from 0.0. Providers that reject sampling rely on the distinction."""
    request = CompletionRequest(
        model="m", messages=[Message.text("user", "hi")], system="s", max_tokens=100
    )
    assert request.temperature is None


def test_completion_request_rejects_a_negative_max_tokens():
    with pytest.raises(PydanticValidationError):
        CompletionRequest(model="m", messages=[], system="s", max_tokens=-1)


def test_usage_totals():
    assert Usage(input_tokens=10, output_tokens=5).total_tokens == 15


@pytest.mark.parametrize(
    ("error_cls", "code", "status"),
    [
        (LLMRateLimitError, "llm_rate_limited", 429),
        (LLMUnavailableError, "llm_unavailable", 503),
        (LLMConfigurationError, "llm_misconfigured", 500),
        (LLMEmptyResponseError, "llm_empty_response", 502),
    ],
)
def test_llm_errors_carry_stable_codes(error_cls, code, status):
    error = error_cls("boom")
    assert error.code == code
    assert error.status_code == status


def test_llm_errors_are_app_errors():
    """So the existing REST and GraphQL handlers render them without changes."""
    assert isinstance(LLMRateLimitError("x"), AppError)
    assert isinstance(LLMRateLimitError("x"), LLMError)

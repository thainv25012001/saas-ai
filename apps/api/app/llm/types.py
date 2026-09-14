from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant"]
ThinkingStyle = Literal["adaptive", "budget", "none"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    """Declared in Phase 2, produced in Phase 4. It exists now so the internal
    message format does not change when tools arrive."""

    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Message(BaseModel):
    role: Role
    content: list[ContentBlock]

    @classmethod
    def text(cls, role: Role, text: str) -> "Message":
        return cls(role=role, content=[TextBlock(text=text)])

    @property
    def text_content(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class CompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    system: str
    max_tokens: int = Field(gt=0, le=128_000)
    # None means "the caller expressed no preference" — distinct from 0.0, and the
    # distinction matters for models that reject sampling parameters entirely.
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    tools: list[ToolSpec] | None = None
    effort: Effort | None = None


class CompletionResponse(BaseModel):
    content: list[ContentBlock]
    usage: Usage
    model: str
    stop_reason: str | None = None

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))


class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    model: str


class TextDeltaEvent(BaseModel):
    type: Literal["text_delta"] = "text_delta"
    text: str


class UsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: Usage


class MessageEndEvent(BaseModel):
    type: Literal["message_end"] = "message_end"
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    model: str = ""


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str


StreamEvent = Annotated[
    MessageStartEvent | TextDeltaEvent | UsageEvent | MessageEndEvent | ErrorEvent,
    Field(discriminator="type"),
]

# Phase 2 — LLM Chat with Streaming: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A business owner opens the playground, types a question, and watches a real LLM's answer stream back — through a provider layer that swaps OpenAI for Anthropic without touching business logic.

**Architecture:** A normalized, block-structured message format modelled on Anthropic's API (the richer of the two) and downcast for OpenAI. Each provider declares per-model capabilities and filters requests against them, because the current Claude models reject `temperature` outright. Conversations and messages are tenant-owned and RLS-protected like every other business table. Streaming reaches the browser over SSE.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, `anthropic` and `openai` SDKs, pydantic; Next.js 15, TypeScript, Tailwind v4.

**Spec:** [docs/PHASE-2.md](../../PHASE-2.md), which extends [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) (§2.2 streaming, §3.5 conversations, §3.8 usage, §5 agent).

## Global Constraints

- Python **3.12**, `uv`; all API code under `apps/api/app/`. `uv` is on PATH.
- **GNU `make` is NOT available on this machine.** Run the underlying commands directly; still add any Makefile target the plan names, as it is a deliverable.
- Async tests use **`anyio` only** — `pytest-asyncio` is deliberately absent. Use `pytestmark = pytest.mark.anyio`.
- **No test may make a network call.** Every test runs against `FakeProvider` or a mocked SDK client. A test that needs an API key is a defect.
- Tenant-owned tables get `organization_id UUID NOT NULL` + RLS via the existing `enable_rls(op, table)` helper. Never inline policy SQL.
- Primary keys are UUIDv7 from `app.core.ids.uuid7()`.
- Money is `Decimal`, never `float`.
- `ruff check .`, `ruff format --check .`, `mypy --strict app/` must pass before every commit. The suite must stay **warning-free**.
- Conventional Commits, ending with exactly:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`
- Baseline: 136 tests passing on `main`.

## Existing interfaces you build on

- `app/core/errors.py` — `AppError` + `NotFoundError`, `ConflictError`, `AuthenticationError`, `PermissionDeniedError`, `ValidationError`, `RateLimitError`. `format_validation_errors(errors)` renders pydantic errors without leaking input values.
- `app/core/tenancy.py` — `TenantContext(organization_id, user_id, role, request_id)`, `tenant_session(tenant)`, `untenanted_session()`.
- `app/core/config.py` — `Settings` / `get_settings()` (`lru_cache`d; call `.cache_clear()` in tests that change env).
- `app/core/ids.py` — `uuid7()`. `app/core/logging.py` — `get_logger(__name__)`, `request_id_var`.
- `app/db/base.py` — `Base`, `UUIDPrimaryKeyMixin`, `TimestampMixin`, `TenantMixin`, `enable_rls` / `disable_rls`.
- `app/agents/service.py` — `AgentService(session, tenant)`: `get_agent(id)`, `get_config(agent_id)`.
- `app/prompts/service.py` — `PromptService(session, tenant)`: `active_version(prompt_id) -> PromptVersion`.
- `app/prompts/defaults.py` — `DEFAULT_SALES_SYSTEM_PROMPT`.
- `agents` columns: `provider` (default `"openai"`), `model` (default `"gpt-4o-mini"`), `temperature` (default `0.3`), `max_tokens` (default `1024`), `prompt_id`.
- `agent_configs` columns: `persona`, `tone`, `language`, `greeting`, `fallback_message`, `max_agent_steps`, `variables`.
- Migrations are at head `0004_prompts`. Yours is `0005_conversations`.
- Tests: `tests/conftest.py` has `anyio_backend`, `client`, `owner_connection`, `clean_users`, `tenant_a`, `tenant_b`. `tests/integration/conftest.py` disposes the DB engine and Redis client after each test.

## File Structure

```text
apps/api/app/
├── llm/
│   ├── types.py           # Task 1 — Message, ContentBlock, StreamEvent, requests
│   ├── errors.py          # Task 1 — LLM* domain errors
│   ├── base.py            # Task 1 — LLMProvider protocol, ModelCapabilities
│   ├── pricing.py         # Task 2 — per-model USD/MTok table, Decimal cost
│   ├── fake_provider.py   # Task 2 — deterministic, no network
│   ├── registry.py        # Task 2 — name → provider instance
│   ├── anthropic_provider.py  # Task 3
│   └── openai_provider.py     # Task 4
├── conversations/
│   ├── schemas.py         # Task 5
│   └── service.py         # Task 5
├── chat/
│   └── service.py         # Task 6 — orchestration
├── db/models/conversation.py  # Task 5
├── api/chat.py            # Task 7 — SSE endpoint
apps/web/src/app/dashboard/playground/page.tsx  # Task 8
```

---

### Task 1: LLM domain types, errors, and the provider protocol

**Files:**
- Create: `apps/api/app/llm/__init__.py`, `types.py`, `errors.py`, `base.py`
- Test: `apps/api/tests/unit/test_llm_types.py`

**Interfaces produced — every later task depends on these exact names:**
- `Role = Literal["system", "user", "assistant"]`
- `TextBlock(type="text", text: str)`, `ToolUseBlock(type="tool_use", id, name, input)`, `ToolResultBlock(type="tool_result", tool_use_id, content, is_error)` — a discriminated union `ContentBlock` on `type`.
- `Message(role: Role, content: list[ContentBlock])` with `Message.text(role, text)` classmethod and a `.text_content` property joining text blocks.
- `ToolSpec(name, description, input_schema)` — declared now, unused until Phase 4.
- `Usage(input_tokens: int, output_tokens: int)`.
- `CompletionRequest(model, messages, system, max_tokens, temperature=None, tools=None, effort=None)`.
- `CompletionResponse(content: list[ContentBlock], usage: Usage, model, stop_reason, text: str)`.
- Stream events, a union `StreamEvent` discriminated on `type`: `MessageStartEvent`, `TextDeltaEvent(text)`, `UsageEvent(usage)`, `MessageEndEvent(stop_reason, usage, model)`, `ErrorEvent(code, message)`.
- `ModelCapabilities(supports_sampling, supports_thinking, thinking_style, supports_effort, max_output_tokens)`.
- `LLMProvider` Protocol with `name`, `generate`, `stream`, `generate_structured`, `capabilities(model) -> ModelCapabilities`.
- `app/llm/errors.py`: `LLMError(AppError)` and subclasses `LLMRateLimitError` (429, `rate_limited`), `LLMUnavailableError` (503, `llm_unavailable`), `LLMConfigurationError` (500, `llm_misconfigured`), `LLMEmptyResponseError` (502, `llm_empty_response`).

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_llm_types.py`:

```python
import pytest
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
    Message,
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
    message = Message(
        role="assistant",
        content=[TextBlock(text="a"), ToolUseBlock(id="t", name="n", input={})],
    )
    assert [block.type for block in message.content] == ["text", "tool_use"]


def test_completion_request_defaults_temperature_to_none():
    """None means 'the agent did not ask for a specific value', which is different
    from 0.0. Providers that reject sampling rely on the distinction."""
    request = CompletionRequest(
        model="m", messages=[Message.text("user", "hi")], system="s", max_tokens=100
    )
    assert request.temperature is None


def test_completion_request_rejects_a_negative_max_tokens():
    with pytest.raises(PydanticValidationError):
        CompletionRequest(
            model="m", messages=[], system="s", max_tokens=-1
        )


def test_usage_totals():
    assert Usage(input_tokens=10, output_tokens=5).total_tokens == 15


@pytest.mark.parametrize(
    ("error_cls", "code", "status"),
    [
        (LLMRateLimitError, "rate_limited", 429),
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/unit/test_llm_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.llm'`.

- [ ] **Step 3: Write the errors**

Create `apps/api/app/llm/errors.py`:

```python
from app.core.errors import AppError


class LLMError(AppError):
    """Base for every provider failure, normalized away from SDK-specific types.

    Callers never see an `anthropic.*` or `openai.*` exception: the SSE layer and the
    chat service would otherwise need to know which SDK produced a failure in order to
    render it.
    """

    code = "llm_error"
    status_code = 502


class LLMRateLimitError(LLMError):
    code = "rate_limited"
    status_code = 429


class LLMUnavailableError(LLMError):
    """Timeout, dropped connection, or a provider-side 5xx. Retrying may help."""

    code = "llm_unavailable"
    status_code = 503


class LLMConfigurationError(LLMError):
    """A missing or rejected API key, or a model this account cannot reach.

    Deliberately a 500: this is an operator problem, and telling the end user to
    "try again" would be a lie.
    """

    code = "llm_misconfigured"
    status_code = 500


class LLMEmptyResponseError(LLMError):
    code = "llm_empty_response"
    status_code = 502
```

- [ ] **Step 4: Write the types**

Create `apps/api/app/llm/types.py`:

```python
from collections.abc import AsyncIterator  # noqa: F401  (re-exported for providers)
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


ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")
]


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
```

- [ ] **Step 5: Write the provider protocol**

Create `apps/api/app/llm/base.py`:

```python
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    StreamEvent,
    ThinkingStyle,
)

SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """What a specific model will actually accept.

    This exists because the current Claude models REJECT `temperature` with a 400,
    while `agents.temperature` is a column in our schema with a dashboard input behind
    it. Without per-model capabilities, every Anthropic agent would fail on its first
    message. Capabilities are data so adding a model is a table entry, not a branch.
    """

    supports_sampling: bool
    supports_thinking: bool
    thinking_style: ThinkingStyle
    supports_effort: bool
    max_output_tokens: int


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def capabilities(self, model: str) -> ModelCapabilities: ...

    async def generate(self, request: CompletionRequest) -> CompletionResponse: ...

    # Deliberately not `async def`: returns the iterator directly so callers write
    # `async for event in provider.stream(req)` with no extra await.
    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]: ...

    async def generate_structured(
        self, request: CompletionRequest, schema: type[SchemaT]
    ) -> SchemaT: ...
```

Create an empty `apps/api/app/llm/__init__.py`.

- [ ] **Step 6: Run the tests and the gates**

Run: `cd apps/api && uv run pytest tests/unit/test_llm_types.py -v`
Expected: 11 passed.

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict app/`
Expected: clean. If `mypy` objects to the `AsyncIterator` re-export in `types.py`, delete that import — it is a convenience, not a requirement.

- [ ] **Step 7: Commit**

```bash
git add apps/api
git commit -m "feat: add normalized LLM message types and provider protocol"
```

---

### Task 2: Pricing, the fake provider, and the registry

**Files:**
- Create: `apps/api/app/llm/pricing.py`, `fake_provider.py`, `registry.py`
- Modify: `apps/api/app/core/config.py` (add provider settings)
- Test: `apps/api/tests/unit/test_pricing.py`, `apps/api/tests/unit/test_fake_provider.py`, `apps/api/tests/unit/test_registry.py`

**Interfaces produced:**
- `pricing.MODEL_PRICING: dict[str, ModelPrice]` where `ModelPrice(input_per_mtok: Decimal, output_per_mtok: Decimal)`; `pricing.estimate_cost(model, usage) -> Decimal | None` returning `None` for an unknown model after logging a warning.
- `FakeProvider(script: list[str] | None = None, usage: Usage | None = None, fail_with: Exception | None = None)` implementing `LLMProvider` with `name = "fake"`. Deterministic, no network. `fail_with` raises after the first chunk, so mid-stream failure is testable.
- `registry.get_provider(name: str) -> LLMProvider`, `registry.reset_providers()` (clears the cache; tests call it).
- `Settings` gains `openai_api_key: str | None`, `anthropic_api_key: str | None`, `default_llm_provider: str = "fake"`.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_pricing.py`:

```python
from decimal import Decimal

from app.llm.pricing import MODEL_PRICING, estimate_cost
from app.llm.types import Usage


def test_cost_is_decimal_not_float():
    """Money in floats is how a billing column ends up with 0.30000000000000004."""
    cost = estimate_cost("gpt-4o-mini", Usage(input_tokens=1_000_000, output_tokens=0))
    assert isinstance(cost, Decimal)


def test_cost_for_exactly_one_million_input_tokens_is_the_table_rate():
    price = MODEL_PRICING["gpt-4o-mini"]
    cost = estimate_cost("gpt-4o-mini", Usage(input_tokens=1_000_000, output_tokens=0))
    assert cost == price.input_per_mtok


def test_cost_sums_input_and_output():
    price = MODEL_PRICING["gpt-4o-mini"]
    cost = estimate_cost(
        "gpt-4o-mini", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == price.input_per_mtok + price.output_per_mtok


def test_unknown_model_returns_none_rather_than_guessing():
    """A missing price must be visible as NULL, not silently wrong."""
    assert estimate_cost("some-model-we-have-never-priced", Usage()) is None


def test_zero_usage_costs_nothing():
    assert estimate_cost("gpt-4o-mini", Usage()) == Decimal("0")


def test_claude_opus_5_is_priced():
    """The Anthropic default model must be in the table or every Anthropic
    conversation records a NULL cost."""
    assert "claude-opus-5" in MODEL_PRICING
```

Create `apps/api/tests/unit/test_fake_provider.py`:

```python
import pytest

from app.llm.errors import LLMUnavailableError
from app.llm.fake_provider import FakeProvider
from app.llm.types import CompletionRequest, Message, Usage

pytestmark = pytest.mark.anyio


def _request() -> CompletionRequest:
    return CompletionRequest(
        model="fake-1",
        messages=[Message.text("user", "hello")],
        system="you are a test",
        max_tokens=100,
    )


async def test_stream_emits_start_deltas_and_end_in_order():
    provider = FakeProvider(script=["Hel", "lo!"])
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "text_delta", "usage", "message_end"]


async def test_stream_text_reassembles_to_the_script():
    provider = FakeProvider(script=["Hel", "lo!"])
    text = "".join(
        event.text async for event in provider.stream(_request())
        if event.type == "text_delta"
    )
    assert text == "Hello!"


async def test_generate_returns_the_same_text_as_the_stream():
    """The two entry points must not drift — callers choose between them freely."""
    provider = FakeProvider(script=["a", "b"])
    response = await provider.generate(_request())
    assert response.text == "ab"


async def test_reported_usage_is_what_was_configured():
    provider = FakeProvider(script=["x"], usage=Usage(input_tokens=7, output_tokens=3))
    response = await provider.generate(_request())
    assert response.usage.input_tokens == 7
    assert response.usage.output_tokens == 3


async def test_fail_with_raises_after_some_output_has_streamed():
    """Mid-stream failure is the awkward case the chat service must handle: some
    tokens already reached the browser. This is how we reproduce it."""
    provider = FakeProvider(script=["partial ", "more"], fail_with=LLMUnavailableError("gone"))
    seen: list[str] = []
    with pytest.raises(LLMUnavailableError):
        async for event in provider.stream(_request()):
            if event.type == "text_delta":
                seen.append(event.text)
    assert seen == ["partial "]


async def test_capabilities_are_permissive():
    provider = FakeProvider()
    caps = provider.capabilities("fake-1")
    assert caps.supports_sampling is True


async def test_last_request_is_recorded_for_assertions():
    """Tests need to assert on what the caller sent — e.g. that the system prompt
    was rendered from the active prompt version."""
    provider = FakeProvider(script=["x"])
    await provider.generate(_request())
    assert provider.last_request is not None
    assert provider.last_request.system == "you are a test"
```

Create `apps/api/tests/unit/test_registry.py`:

```python
import pytest

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.registry import get_provider, reset_providers


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_providers()
    get_settings.cache_clear()
    yield
    reset_providers()
    get_settings.cache_clear()


def test_fake_provider_resolves_without_any_api_key():
    assert get_provider("fake").name == "fake"


def test_unknown_provider_name_is_a_validation_error():
    with pytest.raises(ValidationError):
        get_provider("definitely-not-a-provider")


def test_providers_are_cached_per_name():
    """Constructing an SDK client per request would leak connection pools."""
    assert get_provider("fake") is get_provider("fake")


def test_openai_without_a_key_is_a_configuration_error(monkeypatch):
    from app.llm.errors import LLMConfigurationError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    reset_providers()
    with pytest.raises(LLMConfigurationError):
        get_provider("openai")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/unit/test_pricing.py tests/unit/test_fake_provider.py tests/unit/test_registry.py -v`
Expected: FAIL — modules do not exist.

- [ ] **Step 3: Write the pricing table**

Create `apps/api/app/llm/pricing.py`:

```python
from dataclasses import dataclass
from decimal import Decimal

from app.core.logging import get_logger
from app.llm.types import Usage

logger = get_logger(__name__)

# Prices are USD per MILLION tokens. Verified 2026-09-14 against the providers'
# public pricing pages. An entry going stale is a billing error, so the date above
# is part of the data, not decoration.
_MTOK = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


MODEL_PRICING: dict[str, ModelPrice] = {
    # Anthropic
    "claude-opus-5": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPrice(Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": ModelPrice(Decimal("1.00"), Decimal("5.00")),
    # OpenAI
    "gpt-4o": ModelPrice(Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": ModelPrice(Decimal("0.15"), Decimal("0.60")),
    # Local/testing
    "fake-1": ModelPrice(Decimal("0"), Decimal("0")),
}


def estimate_cost(model: str, usage: Usage) -> Decimal | None:
    """Cost in USD, or None when the model is not priced.

    Returning None rather than 0 is deliberate: an unpriced model must show up as a
    NULL in the database, where it is visible, instead of as a free request, where it
    silently understates the bill.
    """
    price = MODEL_PRICING.get(model)
    if price is None:
        logger.warning("model_not_priced", model=model)
        return None

    return (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
    ) / _MTOK
```

- [ ] **Step 4: Write the fake provider**

Create `apps/api/app/llm/fake_provider.py`:

```python
from collections.abc import AsyncIterator

from pydantic import BaseModel

from app.llm.base import ModelCapabilities
from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    ErrorEvent,  # noqa: F401  (kept for symmetry with the real providers)
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    Usage,
    UsageEvent,
)

_DEFAULT_SCRIPT = [
    "Thanks for asking! ",
    "I am a placeholder response ",
    "until a real provider is configured.",
]


class FakeProvider:
    """A deterministic provider with no network calls.

    This is production code, not a test fixture, for two reasons. Every test in this
    phase runs against it, so the suite is fast, free and reproducible for anyone who
    clones the repo without an API key — a suite that only runs for people with billing
    configured stops being run. And it is what `ENVIRONMENT=local` falls back to when no
    key is set, so the playground works out of the box.
    """

    name = "fake"

    def __init__(
        self,
        script: list[str] | None = None,
        usage: Usage | None = None,
        fail_with: Exception | None = None,
    ) -> None:
        self._script = script if script is not None else list(_DEFAULT_SCRIPT)
        self._usage = usage or Usage(input_tokens=10, output_tokens=5)
        self._fail_with = fail_with
        self.last_request: CompletionRequest | None = None

    def capabilities(self, model: str) -> ModelCapabilities:
        return ModelCapabilities(
            supports_sampling=True,
            supports_thinking=False,
            thinking_style="none",
            supports_effort=False,
            max_output_tokens=4096,
        )

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        self.last_request = request
        if self._fail_with is not None:
            raise self._fail_with
        return CompletionResponse(
            content=[TextBlock(text="".join(self._script))],
            usage=self._usage,
            model=request.model,
            stop_reason="end_turn",
        )

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        self.last_request = request
        yield MessageStartEvent(model=request.model)
        for index, chunk in enumerate(self._script):
            # Fail AFTER the first chunk so callers can reproduce the case where
            # output has already reached the browser before the provider dies.
            if self._fail_with is not None and index == 1:
                raise self._fail_with
            yield TextDeltaEvent(text=chunk)
        if self._fail_with is not None and len(self._script) <= 1:
            raise self._fail_with
        yield UsageEvent(usage=self._usage)
        yield MessageEndEvent(
            stop_reason="end_turn", usage=self._usage, model=request.model
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def generate_structured(
        self, request: CompletionRequest, schema: type[BaseModel]
    ) -> BaseModel:
        self.last_request = request
        return schema()
```

- [ ] **Step 5: Extend Settings and write the registry**

Modify `apps/api/app/core/config.py`, adding these fields to `Settings` (keep everything else untouched):

```python
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # `fake` keeps the playground working with no key configured. Set to
    # `openai` or `anthropic` once a key is present.
    default_llm_provider: str = "fake"
```

Create `apps/api/app/llm/registry.py`:

```python
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.base import LLMProvider
from app.llm.errors import LLMConfigurationError
from app.llm.fake_provider import FakeProvider

_PROVIDERS: dict[str, LLMProvider] = {}
_KNOWN = ("fake", "openai", "anthropic")


def get_provider(name: str) -> LLMProvider:
    """Resolve a provider by name, constructing it at most once.

    Cached because each real provider holds an SDK client with its own connection
    pool; building one per request would leak sockets under load.
    """
    if name not in _KNOWN:
        raise ValidationError(f"unknown provider '{name}'; expected one of {', '.join(_KNOWN)}")

    cached = _PROVIDERS.get(name)
    if cached is not None:
        return cached

    provider = _build(name)
    _PROVIDERS[name] = provider
    return provider


def _build(name: str) -> LLMProvider:
    settings = get_settings()
    if name == "fake":
        return FakeProvider()

    if name == "openai":
        if not settings.openai_api_key:
            raise LLMConfigurationError(
                "OPENAI_API_KEY is not set; set it or use the 'fake' provider"
            )
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key)

    if not settings.anthropic_api_key:
        raise LLMConfigurationError(
            "ANTHROPIC_API_KEY is not set; set it or use the 'fake' provider"
        )
    from app.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key=settings.anthropic_api_key)


def reset_providers() -> None:
    """Drop cached providers. Tests call this after changing settings."""
    _PROVIDERS.clear()
```

> The imports of the real providers are function-local on purpose: `app.llm.registry` must import cleanly before Tasks 3 and 4 exist, and at runtime a missing optional SDK should not break the fake path.

- [ ] **Step 6: Add `.env.example` entries**

Append to `.env.example` (repo root), with the comment:

```bash
# ---- LLM providers --------------------------------------------------------
# Leave both unset to use the deterministic `fake` provider, which needs no
# network and makes the playground work out of the box.
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
DEFAULT_LLM_PROVIDER=fake
```

- [ ] **Step 7: Run the tests and gates, then commit**

Run: `cd apps/api && uv run pytest tests/unit -v` — report the actual count.
Run the three gates; all clean.

```bash
git add apps/api .env.example
git commit -m "feat: add model pricing, deterministic fake provider, and provider registry"
```

---

### Task 3: The Anthropic provider

**Files:**
- Create: `apps/api/app/llm/anthropic_provider.py`
- Modify: `apps/api/pyproject.toml` (add `anthropic`)
- Test: `apps/api/tests/unit/test_anthropic_provider.py`

**Interfaces produced:** `AnthropicProvider(api_key: str)` implementing `LLMProvider`, `name = "anthropic"`.

**This is the task the whole abstraction exists for. Read carefully:**

Claude Opus 5, Opus 4.8, Opus 4.7 and Sonnet 5 **reject `temperature` with a 400**. Sampling parameters were removed from those models. Our `agents.temperature` column defaults to `0.3` and the dashboard has an input for it, so an adapter that forwards every field would make every Anthropic agent fail on its first message.

The same models handle reasoning differently: thinking is **adaptive and on by default**, depth is controlled by `output_config.effort`, and `budget_tokens` is rejected.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_anthropic_provider.py`. These tests mock the SDK client — **no network**.

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.types import CompletionRequest, Message

pytestmark = pytest.mark.anyio


def _request(**overrides) -> CompletionRequest:
    payload = {
        "model": "claude-opus-5",
        "messages": [Message.text("user", "hello")],
        "system": "you are a sales assistant",
        "max_tokens": 1024,
    }
    payload.update(overrides)
    return CompletionRequest(**payload)


class _FakeStream:
    """Stands in for the SDK's async streaming context manager."""

    def __init__(self, events, final):
        self._events, self._final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        async def gen():
            for event in self._events:
                yield event

        return gen()

    async def get_final_message(self):
        return self._final


def _text_delta(text):
    event = MagicMock()
    event.type = "content_block_delta"
    event.delta.type = "text_delta"
    event.delta.text = text
    return event


def _final_message(input_tokens=10, output_tokens=4, stop_reason="end_turn"):
    final = MagicMock()
    final.usage.input_tokens = input_tokens
    final.usage.output_tokens = output_tokens
    final.stop_reason = stop_reason
    return final


def _provider_with(stream):
    provider = AnthropicProvider(api_key="test-key")
    provider._client = MagicMock()          # noqa: SLF001 — substituting the SDK client
    provider._client.messages.stream = MagicMock(return_value=stream)  # noqa: SLF001
    return provider


async def test_temperature_is_not_sent_to_a_model_that_rejects_it():
    """THE test for this task. Sending temperature to claude-opus-5 is a 400."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request(temperature=0.7)):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "temperature" not in kwargs


async def test_thinking_is_adaptive_for_current_models():
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["thinking"] == {"type": "adaptive"}


async def test_budget_tokens_is_never_sent():
    """It is rejected with a 400 on every model we default to."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert "budget_tokens" not in str(kwargs.get("thinking", {}))


async def test_system_prompt_is_sent_as_the_system_parameter_not_a_message():
    """Anthropic takes the system prompt top-level; putting it in `messages` as a
    system-role entry is rejected."""
    provider = _provider_with(_FakeStream([_text_delta("hi")], _final_message()))
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["system"] == "you are a sales assistant"
    assert all(m["role"] != "system" for m in kwargs["messages"])


async def test_stream_yields_normalized_events_in_order():
    provider = _provider_with(
        _FakeStream([_text_delta("He"), _text_delta("llo")], _final_message())
    )
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "text_delta", "usage", "message_end"]


async def test_usage_is_taken_from_the_final_message():
    provider = _provider_with(
        _FakeStream([_text_delta("x")], _final_message(input_tokens=99, output_tokens=7))
    )
    end = [e async for e in provider.stream(_request()) if e.type == "message_end"][0]
    assert end.usage.input_tokens == 99
    assert end.usage.output_tokens == 7


async def test_effort_is_forwarded_when_the_model_supports_it():
    provider = _provider_with(_FakeStream([_text_delta("x")], _final_message()))
    async for _ in provider.stream(_request(effort="low")):
        pass
    kwargs = provider._client.messages.stream.call_args.kwargs  # noqa: SLF001
    assert kwargs["output_config"] == {"effort": "low"}


def test_capabilities_report_no_sampling_for_current_models():
    provider = AnthropicProvider(api_key="k")
    assert provider.capabilities("claude-opus-5").supports_sampling is False


def test_capabilities_report_sampling_for_older_models():
    """Haiku 4.5 still accepts temperature; the table must not over-generalize."""
    provider = AnthropicProvider(api_key="k")
    assert provider.capabilities("claude-haiku-4-5").supports_sampling is True


def test_unknown_model_falls_back_to_the_conservative_capability_set():
    """Guessing 'it probably supports sampling' would produce a 400 on a new model.
    The safe default is to send less."""
    provider = AnthropicProvider(api_key="k")
    assert provider.capabilities("claude-something-new").supports_sampling is False
```

- [ ] **Step 2: Run the tests to verify they fail, then add the dependency**

Run: `cd apps/api && uv run pytest tests/unit/test_anthropic_provider.py -v`
Expected: FAIL — module missing.

Add to `apps/api/pyproject.toml` `dependencies`: `"anthropic>=0.40"`. Run `uv sync`.

- [ ] **Step 3: Write the provider**

Create `apps/api/app/llm/anthropic_provider.py`. The error-mapping block must catch the SDK's own exception types — **verify the exact class names against the installed SDK** (`uv run python -c "import anthropic; print([n for n in dir(anthropic) if 'Error' in n])"`) and adjust if they differ from what is written here.

```python
from collections.abc import AsyncIterator
from typing import Any

import anthropic
from pydantic import BaseModel

from app.core.logging import get_logger
from app.llm.base import ModelCapabilities
from app.llm.errors import (
    LLMConfigurationError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.llm.types import (
    CompletionRequest,
    CompletionResponse,
    MessageEndEvent,
    MessageStartEvent,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    Usage,
    UsageEvent,
)

logger = get_logger(__name__)

# Sampling was REMOVED from these models: sending `temperature` returns a 400.
# Thinking is adaptive and on by default; depth is set with output_config.effort.
_NO_SAMPLING = ModelCapabilities(
    supports_sampling=False,
    supports_thinking=True,
    thinking_style="adaptive",
    supports_effort=True,
    max_output_tokens=64_000,
)

# Older models keep the classic sampling + explicit thinking-budget surface.
_CLASSIC = ModelCapabilities(
    supports_sampling=True,
    supports_thinking=True,
    thinking_style="budget",
    supports_effort=False,
    max_output_tokens=8_192,
)

_CAPABILITIES: dict[str, ModelCapabilities] = {
    "claude-opus-5": _NO_SAMPLING,
    "claude-sonnet-5": _NO_SAMPLING,
    "claude-opus-4-8": _NO_SAMPLING,
    "claude-haiku-4-5": _CLASSIC,
}


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def capabilities(self, model: str) -> ModelCapabilities:
        # An unknown model gets the CONSERVATIVE set, not the permissive one.
        # Guessing that a new model accepts sampling produces a 400 on first use;
        # guessing it does not merely sends less than we could have.
        return _CAPABILITIES.get(model, _NO_SAMPLING)

    def _build_kwargs(self, request: CompletionRequest) -> dict[str, Any]:
        caps = self.capabilities(request.model)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": min(request.max_tokens, caps.max_output_tokens),
            # Anthropic takes the system prompt top-level, NOT as a message.
            "system": request.system,
            "messages": [
                {"role": m.role, "content": m.text_content}
                for m in request.messages
                if m.role != "system"
            ],
        }

        if request.temperature is not None:
            if caps.supports_sampling:
                kwargs["temperature"] = request.temperature
            else:
                logger.debug(
                    "dropped_unsupported_parameter",
                    parameter="temperature",
                    model=request.model,
                )

        if caps.supports_thinking and caps.thinking_style == "adaptive":
            kwargs["thinking"] = {"type": "adaptive"}

        if request.effort is not None and caps.supports_effort:
            kwargs["output_config"] = {"effort": request.effort}

        return kwargs

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        kwargs = self._build_kwargs(request)
        try:
            async with self._client.messages.stream(**kwargs) as stream:
                yield MessageStartEvent(model=request.model)
                async for event in stream:
                    if (
                        getattr(event, "type", None) == "content_block_delta"
                        and getattr(event.delta, "type", None) == "text_delta"
                    ):
                        yield TextDeltaEvent(text=event.delta.text)

                final = await stream.get_final_message()
                usage = Usage(
                    input_tokens=final.usage.input_tokens,
                    output_tokens=final.usage.output_tokens,
                )
                yield UsageEvent(usage=usage)
                yield MessageEndEvent(
                    stop_reason=final.stop_reason, usage=usage, model=request.model
                )
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError("the model provider is rate limiting us") from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError) as exc:
            raise LLMUnavailableError("could not reach the model provider") from exc
        except anthropic.APIStatusError as exc:
            # 401/403 is an operator problem (bad or missing key), not a user one.
            if exc.status_code in (401, 403):
                raise LLMConfigurationError(
                    "the model provider rejected our credentials"
                ) from exc
            raise LLMUnavailableError(
                f"the model provider returned {exc.status_code}"
            ) from exc

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def generate(self, request: CompletionRequest) -> CompletionResponse:
        parts: list[str] = []
        usage = Usage()
        stop_reason: str | None = None
        async for event in self._stream(request):
            if event.type == "text_delta":
                parts.append(event.text)
            elif event.type == "message_end":
                usage, stop_reason = event.usage, event.stop_reason
        return CompletionResponse(
            content=[TextBlock(text="".join(parts))],
            usage=usage,
            model=request.model,
            stop_reason=stop_reason,
        )

    async def generate_structured(
        self, request: CompletionRequest, schema: type[BaseModel]
    ) -> BaseModel:
        raise NotImplementedError(
            "structured output arrives with Phase 4 tool calling"
        )
```

- [ ] **Step 4: Run the tests, gates, and commit**

Run: `cd apps/api && uv run pytest tests/unit/test_anthropic_provider.py -v`
Expected: 10 passed. If an SDK exception class name differs, fix the code — do not weaken a test.

```bash
git add apps/api
git commit -m "feat: add Anthropic provider with per-model capability filtering"
```

---

### Task 4: The OpenAI provider

**Files:**
- Create: `apps/api/app/llm/openai_provider.py`
- Modify: `apps/api/pyproject.toml` (add `openai`)
- Test: `apps/api/tests/unit/test_openai_provider.py`

**Interfaces produced:** `OpenAIProvider(api_key: str)` implementing `LLMProvider`, `name = "openai"`.

**The downcast is the interesting part.** Our internal format is a list of content blocks; OpenAI's Chat Completions takes a string plus a parallel `tool_calls` array. The system prompt is a **message with `role: "system"`**, unlike Anthropic where it is a top-level parameter — get this backwards and one of the two providers silently ignores the agent's persona.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_openai_provider.py`, mirroring the Anthropic test structure so the two adapters stay comparable:

```python
from unittest.mock import MagicMock

import pytest

from app.llm.openai_provider import OpenAIProvider
from app.llm.types import CompletionRequest, Message

pytestmark = pytest.mark.anyio


def _request(**overrides) -> CompletionRequest:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [Message.text("user", "hello")],
        "system": "you are a sales assistant",
        "max_tokens": 512,
    }
    payload.update(overrides)
    return CompletionRequest(**payload)


def _chunk(text=None, usage=None, finish_reason=None):
    chunk = MagicMock()
    if usage is None:
        chunk.usage = None
    else:
        chunk.usage = MagicMock(prompt_tokens=usage[0], completion_tokens=usage[1])
    if text is None and finish_reason is None:
        chunk.choices = []
    else:
        choice = MagicMock()
        choice.delta.content = text
        choice.finish_reason = finish_reason
        chunk.choices = [choice]
    return chunk


def _provider_with(chunks):
    async def _aiter(**_kwargs):
        for chunk in chunks:
            yield chunk

    provider = OpenAIProvider(api_key="test-key")
    create = MagicMock(side_effect=lambda **kw: _aiter(**kw))
    provider._client = MagicMock()                      # noqa: SLF001
    provider._client.chat.completions.create = create   # noqa: SLF001
    return provider


async def test_system_prompt_becomes_the_first_message():
    """OpenAI has no top-level system parameter — it is a message with role=system."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    messages = provider._client.chat.completions.create.call_args.kwargs["messages"]  # noqa: SLF001
    assert messages[0] == {"role": "system", "content": "you are a sales assistant"}
    assert messages[1]["role"] == "user"


async def test_temperature_is_forwarded_because_openai_accepts_it():
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request(temperature=0.7)):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["temperature"] == 0.7


async def test_usage_is_requested_explicitly():
    """OpenAI omits usage from streamed responses unless asked."""
    provider = _provider_with([_chunk("hi", finish_reason="stop")])
    async for _ in provider.stream(_request()):
        pass
    kwargs = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["stream_options"] == {"include_usage": True}


async def test_stream_yields_normalized_events_in_order():
    provider = _provider_with(
        [_chunk("He"), _chunk("llo", finish_reason="stop"), _chunk(usage=(12, 3))]
    )
    types = [event.type async for event in provider.stream(_request())]
    assert types == ["message_start", "text_delta", "text_delta", "usage", "message_end"]


async def test_usage_reaches_message_end():
    provider = _provider_with(
        [_chunk("x", finish_reason="stop"), _chunk(usage=(12, 3))]
    )
    end = [e async for e in provider.stream(_request()) if e.type == "message_end"][0]
    assert end.usage.input_tokens == 12
    assert end.usage.output_tokens == 3


async def test_chunks_with_no_choices_do_not_crash_the_stream():
    """The final usage-only chunk has an empty `choices` list."""
    provider = _provider_with([_chunk(usage=(1, 1))])
    events = [e async for e in provider.stream(_request())]
    assert any(e.type == "message_end" for e in events)


async def test_a_none_content_delta_is_skipped():
    """Role-only opening deltas carry content=None."""
    provider = _provider_with([_chunk(None, finish_reason=None), _chunk("x", finish_reason="stop")])
    text = "".join(
        e.text async for e in provider.stream(_request()) if e.type == "text_delta"
    )
    assert text == "x"


def test_capabilities_report_sampling_support():
    provider = OpenAIProvider(api_key="k")
    caps = provider.capabilities("gpt-4o-mini")
    assert caps.supports_sampling is True
    assert caps.supports_thinking is False
```

- [ ] **Step 2: Run to verify failure, add the dependency**

Add `"openai>=1.50"` to `apps/api/pyproject.toml` dependencies; `uv sync`.

- [ ] **Step 3: Write the provider**

Create `apps/api/app/llm/openai_provider.py`, following the Anthropic adapter's shape. Requirements, each of which a test above pins:

- `capabilities()` returns `supports_sampling=True`, `supports_thinking=False`, `thinking_style="none"`, `supports_effort=False`, `max_output_tokens=16_384`, for every model (a single permissive record is correct here — OpenAI did not remove sampling).
- `_build_kwargs` produces `model`, `messages` (system first, then the conversation downcast to `{"role", "content"}` using `Message.text_content`), `max_tokens`, `stream=True`, `stream_options={"include_usage": True}`, and `temperature` only when `request.temperature is not None`.
- `_stream` yields `MessageStartEvent`, then a `TextDeltaEvent` per non-empty `choice.delta.content`, then — after the loop — `UsageEvent` and `MessageEndEvent` carrying whatever usage arrived. **Guard `chunk.choices` for emptiness before indexing**, because the usage-only final chunk has none.
- Error mapping identical in shape to the Anthropic adapter: rate limit → `LLMRateLimitError`; timeout/connection → `LLMUnavailableError`; 401/403 → `LLMConfigurationError`; other statuses → `LLMUnavailableError`. **Verify the SDK's exception class names against the installed package** (`uv run python -c "import openai; print([n for n in dir(openai) if 'Error' in n])"`) and use what is actually there.
- `generate` and `generate_structured` mirror the Anthropic provider (the latter raises `NotImplementedError` for now).

- [ ] **Step 4: Tests, gates, commit**

```bash
git add apps/api
git commit -m "feat: add OpenAI provider with content-block downcast"
```

---

### Task 5: Conversations, messages, and usage events

**Files:**
- Create: `apps/api/app/db/models/conversation.py`
- Create: `apps/api/alembic/versions/0005_conversations.py`
- Create: `apps/api/app/conversations/__init__.py`, `schemas.py`, `service.py`
- Modify: `apps/api/app/db/models/__init__.py`
- Test: `apps/api/tests/integration/test_conversation_service.py`

**Interfaces produced:**
- `Conversation`: `id`, `organization_id`, `agent_id`, `visitor_id`, `channel: ConversationChannel`, `status: ConversationStatus`, `title`, `summary`, `metadata_`, `started_at`, `last_message_at`, `closed_at`.
- `Message` (ORM — import as `ConversationMessage` where `app.llm.types.Message` is also in scope, to avoid a name collision): `id`, `organization_id`, `conversation_id`, `seq`, `role`, `content`, `content_blocks`, `prompt_version_id`, `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (Numeric(12,6), nullable), `latency_ms`, `finish_reason`, `error`. Unique `(conversation_id, seq)`.
- `UsageEvent`: `id`, `organization_id`, `agent_id`, `conversation_id`, `kind`, `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`.
- `ConversationService(session, tenant)` with `create(agent_id, channel) -> Conversation`, `get(id) -> Conversation` (raises `NotFoundError` cross-tenant), `list_for_agent(agent_id)`, `history(conversation_id, limit) -> list[ConversationMessage]` ordered by `seq`, `next_seq(conversation_id) -> int`, `append_message(...) -> ConversationMessage`, `record_usage(...)`.

**Requirements a reviewer will check:**
- All three tables carry `organization_id` and are RLS-enabled with `enable_rls`. `down_revision = "0004_prompts"`; exactly one head afterwards.
- `cost_usd` is `Numeric(12, 6)` and **nullable** — an unpriced model records NULL, per the spec.
- `seq` is allocated per conversation and unique with it; `next_seq` must not race two concurrent appends into the same number. Use `SELECT COALESCE(MAX(seq), 0) + 1` inside the caller's transaction and rely on the unique constraint as the backstop, raising `ConflictError` on `IntegrityError`.
- Cross-tenant `get`/`history` raise `NotFoundError`, never `PermissionDeniedError`.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/integration/test_conversation_service.py` covering, at minimum:

1. `create` returns a conversation whose `organization_id` is the tenant's.
2. `get` from another tenant raises `NotFoundError` (use `tenant_a` / `tenant_b`).
3. `append_message` assigns `seq` 1 then 2 for successive messages.
4. `history` returns messages ordered by `seq` ascending, not by insertion order — insert out of order via two appends and assert the ordering.
5. `history(limit=N)` returns the **most recent** N, still in ascending order. (This is the windowing the chat service depends on; returning the *oldest* N would silently give the model stale context.)
6. `cost_usd=None` round-trips as NULL rather than 0.
7. `cost_usd=Decimal("0.001234")` round-trips exactly — assert equality against `Decimal`, which catches a float column.
8. `list_for_agent` from another tenant returns `[]`.
9. `record_usage` writes a row scoped to the tenant.

Each test needs an agent; create one with `AgentService(session, tenant).create_agent(CreateAgentInput(name="..."))`.

- [ ] **Step 2: Write the models, migration, and service**

Follow `app/db/models/agent.py` and `alembic/versions/0004_prompts.py` as the pattern for mixins, enum handling (`values_callable`), index naming (`ix_<table>_organization_id`), and `enable_rls` / `disable_rls` placement. Enums: `ConversationChannel` (`playground`, `widget`, `api`), `ConversationStatus` (`open`, `closed`), `MessageRole` (`user`, `assistant`, `system`, `tool`), `UsageKind` (`llm`, `embedding`).

`metadata` is reserved by SQLAlchemy's declarative base — name the attribute `metadata_` with `mapped_column("metadata", JSONB, ...)`.

- [ ] **Step 3: Migrate, test, gates, commit**

Run `uv run alembic upgrade head`, then `uv run alembic heads` (expect exactly one), then the suite.

```bash
git add apps/api
git commit -m "feat: add conversations, messages, and usage events"
```

---

### Task 6: The chat service

**Files:**
- Create: `apps/api/app/chat/__init__.py`, `apps/api/app/chat/service.py`
- Test: `apps/api/tests/integration/test_chat_service.py`

**Interfaces produced:**
- `ChatService(session, tenant, provider_override: LLMProvider | None = None)`.
- `async def send(agent_id, user_text, conversation_id=None) -> AsyncIterator[ChatEvent]` — creates or loads the conversation, persists the user message, streams the assistant reply, persists it with usage and cost, and yields transport-agnostic events.
- `ChatEvent` union: `ChatMessageStart(conversation_id, message_id)`, `ChatTextDelta(text)`, `ChatMessageEnd(usage, cost_usd, latency_ms, model, prompt_version_id)`, `ChatError(code, message)`.

**What it must do, in order:**

1. Load the agent (404s cross-tenant) and its config.
2. Resolve the system prompt: if `agent.prompt_id` is set, take `PromptService.active_version(...)` and render it; otherwise fall back to `DEFAULT_SALES_SYSTEM_PROMPT`. Record the `prompt_version_id` used (or `None` for the fallback).
3. Render template variables — at minimum `{{company_name}}` (the organization's name) and `{{agent_name}}`. Use a plain, explicit substitution over the declared variables; **do not** use `str.format` (a stray `{` in prompt text would raise) and do not use Jinja with an unsandboxed environment.
4. Create the conversation if `conversation_id` is `None`; otherwise load it (404s cross-tenant).
5. Persist the user message.
6. Build the request: system prompt, then `history(limit=config_window)` mapped to `llm.types.Message`, then the new user text. Resolve the provider via `registry.get_provider(agent.provider)` unless `provider_override` was passed.
7. Stream, accumulating text; yield deltas as they arrive.
8. On success persist the assistant message with `input_tokens`, `output_tokens`, `cost_usd` (from `estimate_cost`), `latency_ms`, `model`, `provider`, `prompt_version_id`, and write a `usage_events` row. Update `conversation.last_message_at`.
9. **On mid-stream failure, persist the partial assistant message with `error` set, then yield `ChatError`.** Do not discard it — the user already saw those tokens on screen, and a history that disagrees with the screen is worse than an incomplete one.

**Tests (all against `FakeProvider` — no network):**

1. A first message creates a conversation and returns its id in `ChatMessageStart`.
2. Deltas concatenate to the fake provider's script.
3. Both the user and assistant messages are persisted, with `seq` 1 and 2.
4. The assistant message records `cost_usd` matching `estimate_cost` for the fake usage.
5. The system prompt reaching the provider is the **active prompt version's** text — create a prompt, activate a second version, and assert `provider.last_request.system` contains the v2 text. This is the test that proves prompt versioning is actually wired to the model rather than merely stored.
6. `{{company_name}}` is replaced with the organization's name, and no `{{` remains in the rendered system prompt.
7. A second message in the same conversation sends the prior turns as history — assert `provider.last_request.messages` has the earlier user and assistant turns before the new one.
8. `history_window` is respected: with a window of 2, a fourth message sends only the last 2 prior turns.
9. Mid-stream failure (`FakeProvider(fail_with=...)`) persists a partial assistant message with `error` set and non-empty content, and yields `ChatError`.
10. A cross-tenant `agent_id` raises `NotFoundError` before any conversation is created — assert no conversation row was written.
11. An unpriced model records `cost_usd = None` rather than 0.

- [ ] **Step 3: Gates and commit**

```bash
git add apps/api
git commit -m "feat: add chat service orchestrating prompt, history, and provider streaming"
```

---

### Task 7: The SSE chat endpoint

**Files:**
- Create: `apps/api/app/api/chat.py`
- Modify: `apps/api/app/main.py` (mount the router)
- Test: `apps/api/tests/integration/test_chat_endpoint.py`

**Interfaces produced:** `POST /api/v1/chat/stream`, authenticated with the existing bearer dependency, body `{agent_id: UUID, message: str, conversation_id: UUID | None}`, response `text/event-stream`.

**Requirements:**
- Wire events to the envelope in `docs/PHASE-2.md` §4: `message_start`, `text_delta`, `message_end`, `error`. One JSON object per `data:` line.
- Response headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`, `Connection: keep-alive`. Without these a proxy buffers the whole body and streaming degrades to one delayed blob.
- A `: ping` heartbeat comment every 15 s while the stream is open.
- **Errors raised before the stream starts** (unknown agent, validation) return a normal JSON error envelope with the right status — not a 200 with an error event. **Errors raised mid-stream** must be delivered as an SSE `error` event, because the status line is already sent and cannot be changed.
- Tests override the provider by injecting a `FakeProvider` through a FastAPI dependency override, so no test touches the network.

**Tests:**
1. Unauthenticated → 401 with the JSON envelope, not an event stream.
2. Content type is `text/event-stream`; `X-Accel-Buffering: no` is present.
3. The body parses into the expected event sequence, and the deltas reassemble to the script.
4. `message_end` carries `usage`, `cost_usd`, `model` and `prompt_version_id`.
5. An unknown `agent_id` → 404 JSON envelope, **not** a 200 stream.
6. A cross-tenant `agent_id` → 404 (org B cannot chat with org A's agent). This is the tenancy assertion for the new surface.
7. Mid-stream provider failure → the stream contains an `error` event with `code: "llm_unavailable"` after at least one `text_delta`.
8. A second request with the returned `conversation_id` continues the same conversation rather than creating a new one.

- [ ] **Step 3: Gates and commit**

```bash
git add apps/api
git commit -m "feat: add SSE chat streaming endpoint"
```

---

### Task 8: The playground

**Files:**
- Create: `apps/web/src/lib/sse.ts`, `apps/web/src/components/chat/ChatMessage.tsx`
- Modify: `apps/web/src/app/dashboard/playground/page.tsx` (replace the placeholder)
- Modify: `apps/web/src/app/dashboard/agents/[id]/page.tsx` (add a "Test in playground" link)

**Requirements:**
- `lib/sse.ts` exports `streamChat({agentId, message, conversationId, accessToken, onEvent, signal})`, reading the response body with `fetch` + `ReadableStream` and parsing SSE frames. **`EventSource` cannot be used** — it is GET-only and cannot send an `Authorization` header.
- Buffer across chunk boundaries: a `data:` line can be split across two network reads. Parse on `\n\n` frame boundaries, not per chunk. Ignore lines starting with `:` (heartbeats).
- The page: an agent selector (from the `Agents` query), a transcript, a composer, and a stop button that aborts via `AbortSignal`.
- The assistant message renders progressively as deltas arrive. After `message_end`, show a small footer with model, token counts and cost.
- On an `error` event, show the message in a `role="alert"` region and keep whatever partial text arrived — do not blank it.
- Gate the agent query on `loading` from `useAuth()`, as every other dashboard page does.
- Disable the composer while a stream is in flight.
- Empty state when the organization has no agents: link to `/dashboard/agents`.

- [ ] **Verification**

`npm run typecheck`, `npm run lint`, `npm run build` all clean; backend suite still green. Exercise the flow manually against the running stack with the seeded demo account and the `fake` provider, and describe what you saw.

```bash
git add apps/web
git commit -m "feat: add streaming playground"
```

---

## Phase 2 exit criteria

- [ ] `docker compose up -d --wait` still reaches a working stack.
- [ ] With no API key configured, the playground streams a reply from `FakeProvider`.
- [ ] Setting `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` plus the agent's `provider` switches providers with no code change.
- [ ] Every assistant message persists usage, cost and `prompt_version_id`.
- [ ] Editing an agent's prompt to a new active version changes the next reply's system prompt.
- [ ] Backend suite green and warning-free; all gates clean; one alembic head.
- [ ] No test makes a network call.

## Plan self-review

**Spec coverage.** `docs/PHASE-2.md` §2 (provider abstraction) → Tasks 1–4; §2.3 (capabilities) → Task 3 specifically; §3 (errors) → Task 1 + each adapter; §4 (SSE transport) → Task 7; §5 (cost) → Task 2 + Task 6; §6 (history) → Tasks 5–6. `docs/ARCHITECTURE.md` §3.5 → Task 5; §9's "Phase 2" row → Task 8.

**Deliberately deferred, and why:** GraphQL queries for conversations (the playground drives everything through SSE and does not need them), the prompt-management UI (the API is complete and tested; the screen is cosmetic), history summarization (the playground cannot produce conversations long enough to need it), and `generate_structured` (it has no consumer until Phase 4 tool calling — building it now would repeat the dead-code mistake already corrected once in Phase 1).

**Known weak spot.** Neither real provider can be verified end to end in this environment: there is no API key. The adapters are unit-tested against mocked SDK clients, which pins the request shape — the thing most likely to be wrong — but not the response parsing against a live API. The first real key will be the real test, and the capability filtering in Task 3 is where I would look first if something fails.


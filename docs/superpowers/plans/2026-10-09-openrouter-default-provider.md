# OpenRouter Default Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the offline `fake` LLM provider from the running app, make OpenRouter the default provider, and give real providers structured output so the supplier quote pipeline's AI steps return suggestions.

**Architecture:** A new `app/llm/structured.py` asks any provider for one JSON object through its ordinary `generate()` and validates the reply with Pydantic; OpenAI, OpenRouter (inherits OpenAI) and Anthropic delegate `generate_structured` to it. `FakeProvider` moves to `apps/api/tests/llm_doubles.py`, and an autouse fixture puts it in the registry cache as `openrouter`, so tests stay offline. Migration `0022` moves stored `fake` agents to OpenRouter's default free model.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy async, Alembic, structlog, pytest + anyio, ruff, mypy; Next.js, TypeScript, Vitest.

**Spec:** `docs/superpowers/specs/2026-10-09-openrouter-default-provider-design.md`

## Global Constraints

- `KNOWN_PROVIDERS = ("openai", "anthropic", "openrouter")`, in that order.
- `Settings.default_llm_provider: Literal["openai", "anthropic", "openrouter"] = "openrouter"`.
- `Settings.supplier_quote_llm_model: str | None = None` (max length 100; an empty string means unset).
- `Settings.gmail_llm_matches_per_message: int = 5`, between 0 and 25.
- Structured-output failures raise `LLMStructuredOutputError` (`code = "llm_structured_output_invalid"`, `status_code = 502`). Logs carry the model and the error type, never the reply text.
- Migration `0022` writes the literal model `google/gemma-4-31b-it:free`. Its revision id is `0022_agents_off_fake` (Alembic's `version_num` column holds 32 characters).
- Tests use the model id `test/model:free` (the `:free` suffix prices it at 0, not NULL) and `test/other:free` where a second id is needed.
- After Task 3, no file under `apps/api/app` or `apps/web/src` names `fake` as a provider. `FakeProvider` exists only in `apps/api/tests/llm_doubles.py`.
- Code, comments, docs and commit messages are in English. Every commit message ends with:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- Another session commits to this branch. Locate code by its content, not by the line numbers quoted here, and re-read a file right before editing it.

## Test Environment

The gitignored repo-root `.env` uses Docker hostnames, and it holds a real `OPENROUTER_API_KEY`. Each Bash call is a fresh shell, so prefix API test runs with the exports:

```bash
cd /c/code/saas-ai/apps/api && \
export DATABASE_URL="postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai" \
       MIGRATION_DATABASE_URL="postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai" \
       REDIS_URL="redis://localhost:6379/0" && \
uv run pytest <args>
```

Integration tests write rows into the same `saas_ai` database the dev app uses. Postgres and Redis must be running (`docker compose up -d db redis`).

## Review Focus

1. **A stale `DEFAULT_LLM_PROVIDER=fake`** in someone's `.env` (it was in `.env.example`): startup must fail with a validation error naming `default_llm_provider`, not start and fail later. Test in Task 3 (`test_a_stale_fake_default_provider_fails_at_startup`).
2. **The developer's real OpenRouter key leaking into tests**: the suite must never call OpenRouter with it. Task 3's `tests/conftest.py` forces test values; test in Task 3 (`test_the_suite_never_sees_a_real_provider_key`).
3. **Prose wrapped around valid JSON** (`Sure! Here it is: {...}`), a top-level array or `null`: refused as `LLMStructuredOutputError`, never partly parsed. Test in Task 1 (`test_anything_but_one_conforming_object_is_refused`).
4. **`SUPPLIER_QUOTE_LLM_MODEL=` (empty)**, which `docker-compose.yml` passes when the host has it unset: treated as unset, so the default model is used. Test in Task 2 (`test_without_the_setting_the_providers_default_model_is_used`, parametrized over `None` and `""`).
5. **`GMAIL_LLM_MATCHES_PER_MESSAGE=0`**: no product-match LLM call at all, rows still stored and sent to review. Test in Task 2 (`test_ai_product_matching_stops_at_the_configured_cap`, parametrized over 0 and 2).

## File Map

| File | Change | Task |
|---|---|---|
| `apps/api/app/llm/structured.py` | Create: `generate_structured`, `strip_json_fence` | 1 |
| `apps/api/app/llm/errors.py` | Add `LLMStructuredOutputError` | 1 |
| `apps/api/app/llm/openai_provider.py`, `anthropic_provider.py` | `generate_structured` delegates | 1 |
| `apps/api/app/evaluations/judge.py` | Use shared `strip_json_fence` | 1 |
| `apps/api/app/core/config.py` | Two new settings (Task 2); provider default and `Literal` (Task 3) | 2, 3 |
| `apps/api/app/supplier_quotes/classification.py` | Model from `supplier_quote_llm_model` | 2 |
| `apps/api/app/gmail/sync.py` | Match cap from settings | 2 |
| `apps/api/app/llm/fake_provider.py` | Delete (moves to tests) | 3 |
| `apps/api/app/llm/registry.py`, `catalog.py`, `pricing.py` | Drop `fake` | 3 |
| `apps/api/app/agents/*.py`, `app/api/chat.py`, `app/db/models/agent.py`, `app/db/seed.py`, `app/graphql/types.py` | Comments | 3 |
| `apps/api/tests/llm_doubles.py`, `tests/conftest.py`, `tests/factories.py`, test modules | Test double and fixture; switch to `openrouter` | 3 |
| `apps/api/alembic/versions/0022_agents_off_fake.py` | Create | 4 |
| `apps/web/src/lib/providers.ts`, `setup-checklist.ts`, components and tests | Drop `fake` | 5 |
| `.env.example`, `docker-compose.yml`, `README.md`, `docs/PHASE-2.md`, `docs/PHASE-9.md` | Docs and config | 6 |

`docs/PHASE-3.md`, `PHASE-4.md` and `PHASE-8.md` stay unchanged: their remaining mentions are about the test double, embedding test design, or the widget's demo storefront, all still accurate. `docs/DESIGN.md` has no provider content to update.

---

### Task 1: Structured output for real providers

**Files:**
- Create: `apps/api/app/llm/structured.py`
- Modify: `apps/api/app/llm/errors.py`
- Modify: `apps/api/app/llm/openai_provider.py` (method `generate_structured`)
- Modify: `apps/api/app/llm/anthropic_provider.py` (method `generate_structured`)
- Modify: `apps/api/app/evaluations/judge.py` (docstring, `_strip_json_fence`)
- Test: `apps/api/tests/unit/test_structured_output.py` (create)
- Test: `apps/api/tests/unit/test_openrouter_provider.py` (append)
- Test: `apps/api/tests/unit/test_anthropic_provider.py` (append)
- Test: `apps/api/tests/unit/test_eval_judge.py` (one message string)

**Interfaces:**
- Consumes: `LLMProvider.generate(request) -> CompletionResponse` and `CompletionResponse.text` (`app/llm/base.py`, `app/llm/types.py`).
- Produces:
  - `app.llm.structured.generate_structured(provider: LLMProvider, request: CompletionRequest, schema: type[SchemaT]) -> SchemaT`
  - `app.llm.structured.strip_json_fence(text: str) -> str`
  - `app.llm.errors.LLMStructuredOutputError(LLMError)`

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_structured_output.py`:

```python
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


def test_text_without_a_fence_is_only_trimmed():
    assert strip_json_fence('  {"a": 1}\n') == '{"a": 1}'
```

Append to `apps/api/tests/unit/test_openrouter_provider.py`:

```python
async def test_structured_output_runs_through_the_inherited_stream_loop():
    """The supplier pipeline's classification, mapping and product matching all
    call this. Before it existed they silently fell back to "no suggestion"."""
    from pydantic import BaseModel

    class _Answer(BaseModel):
        label: str

    provider = _provider_with(
        [_chunk('{"label": '), _chunk('"supplier"}', finish_reason="stop"), _chunk(usage=(20, 6))]
    )

    result = await provider.generate_structured(_request(), _Answer)

    assert result == _Answer(label="supplier")
    sent = provider._client.chat.completions.create.call_args.kwargs  # noqa: SLF001
    assert sent["messages"][0]["role"] == "system"
    assert "JSON Schema" in sent["messages"][0]["content"]
```

Append to `apps/api/tests/unit/test_anthropic_provider.py`:

```python
async def test_generate_structured_validates_the_generated_reply(monkeypatch):
    from pydantic import BaseModel

    from app.llm.types import CompletionResponse, TextBlock, Usage

    class _Answer(BaseModel):
        label: str

    provider = AnthropicProvider(api_key="test-key")

    async def _generate(request):
        return CompletionResponse(
            content=[TextBlock(text='{"label": "other"}')], usage=Usage(), model=request.model
        )

    monkeypatch.setattr(provider, "generate", _generate)

    assert await provider.generate_structured(_request(), _Answer) == _Answer(label="other")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (with the Test Environment exports): `uv run pytest tests/unit/test_structured_output.py tests/unit/test_openrouter_provider.py tests/unit/test_anthropic_provider.py -q`
Expected: pytest stops at collection with `ModuleNotFoundError: No module named 'app.llm.structured'`. (Once Step 3 creates the module, the two provider tests still fail with `NotImplementedError: structured output arrives with Phase 4 tool calling` until Step 5.)

- [ ] **Step 3: Add the error and the module**

In `apps/api/app/llm/errors.py`, add after `LLMModelRejectedError`:

```python
class LLMStructuredOutputError(LLMError):
    """The model answered, but not with one JSON object the schema accepts:
    an empty reply, prose, or a missing or invalid field. Retrying the same
    model rarely helps, so callers treat it as "no suggestion"."""

    code = "llm_structured_output_invalid"
    status_code = 502
```

Create `apps/api/app/llm/structured.py`:

```python
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
        raise LLMStructuredOutputError(
            "the model reply does not match the expected schema"
        ) from exc


def _log_failure(model: str, error_type: str) -> None:
    # Never the reply itself: it can quote supplier prices or customer text.
    logger.warning("llm_structured_output_invalid", model=model, error_type=error_type)
```

- [ ] **Step 4: Run the module tests**

Run: `uv run pytest tests/unit/test_structured_output.py -q`
Expected: all pass.

- [ ] **Step 5: Delegate from the providers**

In `apps/api/app/llm/openai_provider.py`, add `from app.llm import structured` to the `app.llm` imports, and replace the method body:

```python
    async def generate_structured(
        self, request: CompletionRequest, schema: type[SchemaT]
    ) -> SchemaT:
        # JSON asked for in the prompt and validated by Pydantic; see
        # `app/llm/structured.py` for why not `response_format`.
        return await structured.generate_structured(self, request, schema)
```

Make the same two changes in `apps/api/app/llm/anthropic_provider.py`. `OpenRouterProvider` inherits the OpenAI method and needs no change.

- [ ] **Step 6: Share the fence helper with the judge**

In `apps/api/app/evaluations/judge.py`:

1. Delete the whole `_strip_json_fence` function.
2. Add `from app.llm.structured import strip_json_fence` to the imports.
3. Change `JudgeVerdict.model_validate_json(_strip_json_fence(response.text))` to `JudgeVerdict.model_validate_json(strip_json_fence(response.text))`.
4. In the module docstring, replace:

```
Decision: `generate`, not `generate_structured`. Both `OpenAIProvider.
generate_structured` and `AnthropicProvider.generate_structured` are
`raise NotImplementedError(...)` today (Phase 4 only wired up `generate`/
`stream`; structured output was never built) -- so calling it here would
make the judge a `NotImplementedError` away from working with either real
provider. Structured output is also documented (docs/PHASE-6.md §4) to
return only the parsed schema with no usage attached, and the judge's usage
must be real and billable (its own `usage_events` row, priced like any
other call), so there is nothing to gain by waiting for it even once it
exists. This module instead calls `generate()` with an explicit
JSON-only instruction in the system prompt, reads `CompletionResponse.
usage` (the real, provider-reported figure `generate()` already exposes),
strips a ```json fence if the model wrapped its answer in one anyway, and
```

with:

```
Decision: `generate`, not `generate_structured`. Structured output
(`app/llm/structured.py`) returns only the parsed schema with no usage
attached, and the judge's usage must be real and billable (its own
`usage_events` row, priced like any other call). This module instead calls
`generate()` with an explicit JSON-only instruction in the system prompt,
reads `CompletionResponse.usage` (the real, provider-reported figure
`generate()` already exposes), strips a ```json fence if the model wrapped
its answer in one anyway (`app.llm.structured.strip_json_fence`, shared with
structured output), and
```

In `apps/api/tests/unit/test_eval_judge.py`, change the message `"providers do not implement this yet -- see judge.py"` to `"Judge only calls generate()"`.

- [ ] **Step 7: Run the affected tests**

Run: `uv run pytest tests/unit/test_structured_output.py tests/unit/test_openrouter_provider.py tests/unit/test_anthropic_provider.py tests/unit/test_openai_provider.py tests/unit/test_eval_judge.py -q`
Expected: all pass.

- [ ] **Step 8: Lint and commit**

```bash
cd /c/code/saas-ai/apps/api && uv run ruff check app tests && uv run ruff format app tests && uv run mypy app/
cd /c/code/saas-ai && git add apps/api/app/llm/structured.py apps/api/app/llm/errors.py apps/api/app/llm/openai_provider.py apps/api/app/llm/anthropic_provider.py apps/api/app/evaluations/judge.py apps/api/tests/unit/test_structured_output.py apps/api/tests/unit/test_openrouter_provider.py apps/api/tests/unit/test_anthropic_provider.py apps/api/tests/unit/test_eval_judge.py
git commit -m "$(cat <<'EOF'
feat: give OpenAI, OpenRouter and Anthropic structured output

Ask for one JSON object in the system prompt and validate the reply with
Pydantic. The supplier quote pipeline's classification, mapping and
product matching called generate_structured, which every real provider
left unimplemented, so they always fell back to an empty suggestion.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Supplier pipeline model and request budget

**Files:**
- Modify: `apps/api/app/core/config.py` (after `gmail_poll_interval_minutes`)
- Modify: `apps/api/app/supplier_quotes/classification.py` (`_generate_structured`)
- Modify: `apps/api/app/gmail/sync.py` (`_LLM_MATCHES_PER_MESSAGE`)
- Test: `apps/api/tests/unit/test_config.py`, `tests/unit/test_supplier_classification.py`, `tests/integration/test_gmail_pipeline.py`

**Interfaces:**
- Consumes: `registry.DEFAULT_MODELS`, `registry.get_provider`.
- Produces: `Settings.supplier_quote_llm_model: str | None`, `Settings.gmail_llm_matches_per_message: int`; `classification._supplier_quote_model(provider_name: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/api/tests/unit/test_config.py`:

```python
def test_supplier_llm_settings_default_to_the_free_model_and_five_matches(monkeypatch):
    monkeypatch.delenv("SUPPLIER_QUOTE_LLM_MODEL", raising=False)
    monkeypatch.delenv("GMAIL_LLM_MATCHES_PER_MESSAGE", raising=False)

    settings = _settings()

    assert settings.supplier_quote_llm_model is None
    assert settings.gmail_llm_matches_per_message == 5


@pytest.mark.parametrize("matches", [0, 1, 25])
def test_llm_matches_per_message_accepts_zero_to_twenty_five(matches):
    assert _settings(gmail_llm_matches_per_message=matches).gmail_llm_matches_per_message == matches


@pytest.mark.parametrize("matches", [-1, 26])
def test_llm_matches_per_message_rejects_values_outside_zero_to_twenty_five(matches):
    with pytest.raises(ValidationError):
        _settings(gmail_llm_matches_per_message=matches)


def test_an_empty_supplier_model_is_accepted():
    """docker-compose passes `SUPPLIER_QUOTE_LLM_MODEL=` when the host leaves
    it unset; that must start the app, and means "use the default"."""
    assert _settings(supplier_quote_llm_model="").supplier_quote_llm_model == ""
```

Append to `apps/api/tests/unit/test_supplier_classification.py`:

```python
async def test_the_supplier_quote_model_setting_picks_the_model(monkeypatch):
    from app.core.config import get_settings
    from app.supplier_quotes.classification import MappingSuggestion

    settings = get_settings().model_copy(update={"supplier_quote_llm_model": "vendor/paid-model"})
    monkeypatch.setattr("app.supplier_quotes.classification.get_settings", lambda: settings)
    provider = _CaptureProvider(MappingSuggestion())

    await suggest_mapping(WorkbookInspection(sheets=[]), provider=provider)

    assert provider.request.model == "vendor/paid-model"


@pytest.mark.parametrize("configured", [None, ""])
async def test_without_the_setting_the_providers_default_model_is_used(monkeypatch, configured):
    from app.core.config import get_settings
    from app.llm.registry import DEFAULT_MODELS
    from app.supplier_quotes.classification import MappingSuggestion

    settings = get_settings().model_copy(update={"supplier_quote_llm_model": configured})
    monkeypatch.setattr("app.supplier_quotes.classification.get_settings", lambda: settings)
    provider = _CaptureProvider(MappingSuggestion())
    provider.name = "openrouter"

    await suggest_mapping(WorkbookInspection(sheets=[]), provider=provider)

    assert provider.request.model == DEFAULT_MODELS["openrouter"]
```

Append to `apps/api/tests/integration/test_gmail_pipeline.py`:

```python
@pytest.mark.parametrize("cap", [0, 2])
async def test_ai_product_matching_stops_at_the_configured_cap(
    owner_connection: Any,
    tenant_a: TenantContext,
    fake_gmail: FakeGmail,
    monkeypatch: pytest.MonkeyPatch,
    cap: int,
) -> None:
    """The free OpenRouter tier allows 50 requests a day for the whole
    account; one large workbook must not spend them all."""
    from app.core.config import get_settings
    from app.gmail.sync import run_gmail_sync
    from app.supplier_quotes.classification import ProductMatchSuggestion

    schemas_asked: list[type] = []

    class _CountingProvider:
        name = "openrouter"

        async def generate_structured(self, _request: Any, schema: Any) -> Any:
            schemas_asked.append(schema)
            return schema()

    settings = get_settings().model_copy(update={"gmail_llm_matches_per_message": cap})
    monkeypatch.setattr("app.gmail.sync.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.supplier_quotes.classification.get_provider", lambda _name: _CountingProvider()
    )
    await seed_mailbox(owner_connection, tenant_a)
    fake_gmail.messages["m-1"] = gmail_message("m-1")
    # Five rows that neither an SKU, an alias nor a name can match.
    fake_gmail.attachments["att-1"] = workbook_bytes(
        [(f"NEW-{index}", f"Gizmo {index}", 10) for index in range(5)]
    )
    fake_gmail.history_message_ids = ["m-1"]

    await run_gmail_sync(mailbox_email=_MAILBOX, notification_history_id="200")

    assert schemas_asked.count(ProductMatchSuggestion) == cap
    message = (await message_rows(tenant_a))["m-1"]
    assert message.status == "review_required"
    async with tenant_session(tenant_a) as session:
        report = await report_for(tenant_a, message)
        assert report is not None
        _report, quotes = await SupplierQuoteService(session, tenant_a).get_report(report.id)
    assert len(quotes) == 5
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_supplier_classification.py tests/integration/test_gmail_pipeline.py -q -k "supplier or matches or cap or model"`
Expected: FAIL. `Settings` ignores unknown fields, so the new config tests fail with `AttributeError` or with no `ValidationError` raised; `test_the_supplier_quote_model_setting_picks_the_model` gets `fake-1` instead of `vendor/paid-model`; the cap test counts 5 calls, not 0 or 2. `test_without_the_setting_the_providers_default_model_is_used` already passes and stays as a guard on the fallback.

- [ ] **Step 3: Add the settings**

In `apps/api/app/core/config.py`, add after `gmail_poll_interval_minutes`:

```python
    # The model the supplier pipeline asks for classification, mapping and
    # product matches. Unset (or empty, as docker-compose passes it) means the
    # default provider's default model, a free OpenRouter one. A paid id moves
    # the pipeline off the free tier's 50-requests-a-day limit with no code
    # change.
    supplier_quote_llm_model: str | None = Field(default=None, max_length=100)
    # LLM product-match calls per supplier email. Each one only suggests a
    # match the owner still confirms, so a low cap costs convenience, not
    # correctness, and keeps one large workbook from spending the free tier.
    gmail_llm_matches_per_message: int = Field(default=5, ge=0, le=25)
```

- [ ] **Step 4: Resolve the supplier model from the setting**

In `apps/api/app/supplier_quotes/classification.py`, replace `_generate_structured` up to and including the `request = CompletionRequest(...)` assignment's `model=` line with:

```python
async def _generate_structured(
    provider: LLMProvider | None,
    model: str | None,
    schema: type[BaseModel],
    *,
    system: str,
    data: dict[str, Any],
) -> Any:
    if provider is None:
        try:
            provider = get_provider(get_settings().default_llm_provider)
        except Exception:
            return schema()
    request = CompletionRequest(
        model=model or _supplier_quote_model(provider.name),
        messages=[Message.text("user", json.dumps(data, ensure_ascii=False, default=str))],
        system=system,
        max_tokens=800,
    )
```

Keep the `try: return await provider.generate_structured(...)` block that follows unchanged. Add below the function:

```python
def _supplier_quote_model(provider_name: str) -> str:
    """`SUPPLIER_QUOTE_LLM_MODEL` when set, else the provider's default model."""
    settings = get_settings()
    if settings.supplier_quote_llm_model:
        return settings.supplier_quote_llm_model
    return DEFAULT_MODELS.get(provider_name, DEFAULT_MODELS[settings.default_llm_provider])
```

- [ ] **Step 5: Read the match cap from settings**

In `apps/api/app/gmail/sync.py`, delete these four lines:

```python
#: Rows per message that may ask the LLM for a catalogue match. Exact SKU and
#: remembered matches need no call; past this, rows wait for review rather
#: than turning one large price list into thousands of sequential LLM calls.
_LLM_MATCHES_PER_MESSAGE = 25
```

and replace `llm_matches_left = _LLM_MATCHES_PER_MESSAGE` with:

```python
        # Exact SKU and remembered matches need no call; past this cap
        # (`GMAIL_LLM_MATCHES_PER_MESSAGE`), rows wait for review.
        llm_matches_left = get_settings().gmail_llm_matches_per_message
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_supplier_classification.py tests/integration/test_gmail_pipeline.py -q`
Expected: all pass.

- [ ] **Step 7: Lint and commit**

```bash
cd /c/code/saas-ai/apps/api && uv run ruff check app tests && uv run ruff format app tests && uv run mypy app/
cd /c/code/saas-ai && git add apps/api/app/core/config.py apps/api/app/supplier_quotes/classification.py apps/api/app/gmail/sync.py apps/api/tests/unit/test_config.py apps/api/tests/unit/test_supplier_classification.py apps/api/tests/integration/test_gmail_pipeline.py
git commit -m "$(cat <<'EOF'
feat: configure the supplier pipeline's model and LLM match cap

SUPPLIER_QUOTE_LLM_MODEL picks the model without a code change, and
GMAIL_LLM_MATCHES_PER_MESSAGE (default 5, was a hard-coded 25) keeps one
workbook from spending the free OpenRouter tier's daily requests.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Remove `fake` from the API; tests use a test double

**Files:**
- Delete: `apps/api/app/llm/fake_provider.py` (its code moves to `apps/api/tests/llm_doubles.py`)
- Modify: `apps/api/app/llm/registry.py`, `app/llm/catalog.py`, `app/llm/pricing.py`, `app/core/config.py`
- Modify (comments only): `app/agents/runner.py`, `app/agents/schemas.py`, `app/agents/service.py`, `app/api/chat.py`, `app/db/models/agent.py`, `app/db/seed.py`, `app/graphql/types.py`
- Modify: `apps/api/tests/conftest.py`, `tests/factories.py`, `tests/llm_doubles.py`, every test module that names `fake`
- Test: `apps/api/tests/unit/test_registry.py` (rewrite), `tests/unit/test_config.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `tests.llm_doubles.FakeProvider(script=None, turns=None, usage=None, fail_with=None, name="openrouter")`, plus `FakeToolCall`, `FakeTurn`, `AnsweredByAnotherModel`, `FALLBACK_MODEL` from the same module; `tests.factories.TEST_PROVIDER = "openrouter"`, `TEST_MODEL = "test/model:free"`.

- [ ] **Step 1: Write the failing tests**

Replace the whole of `apps/api/tests/unit/test_registry.py` with:

```python
import pytest

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.registry import KNOWN_PROVIDERS, get_provider, reset_providers


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_providers()
    get_settings.cache_clear()
    yield
    reset_providers()
    get_settings.cache_clear()


def test_the_known_providers_are_the_three_real_ones():
    assert KNOWN_PROVIDERS == ("openai", "anthropic", "openrouter")


def test_fake_is_no_longer_a_provider():
    """It was an offline stand-in the app shipped; it now lives in the tests."""
    with pytest.raises(ValidationError):
        get_provider("fake")


def test_unknown_provider_name_is_a_validation_error():
    with pytest.raises(ValidationError):
        get_provider("definitely-not-a-provider")


def test_providers_are_cached_per_name(monkeypatch):
    """Constructing an SDK client per request would leak connection pools."""
    keyed = get_settings().model_copy(update={"openrouter_api_key": "test-key"})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: keyed)
    reset_providers()
    assert get_provider("openrouter") is get_provider("openrouter")


def test_openai_without_a_key_is_a_configuration_error(monkeypatch):
    """Patch what the registry actually reads, not the ambient environment: a
    test whose result depends on whether the developer happens to have a real
    OPENAI_API_KEY in .env is not a test."""
    from app.llm.errors import LLMConfigurationError

    base = get_settings()
    no_key_settings = base.model_copy(update={"openai_api_key": None})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: no_key_settings)
    reset_providers()
    with pytest.raises(LLMConfigurationError):
        get_provider("openai")


def test_openrouter_without_a_key_is_a_configuration_error(monkeypatch):
    from app.llm.errors import LLMConfigurationError

    base = get_settings()
    no_key_settings = base.model_copy(update={"openrouter_api_key": None})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: no_key_settings)
    reset_providers()
    with pytest.raises(LLMConfigurationError, match="OPENROUTER_API_KEY is not set"):
        get_provider("openrouter")


def test_openrouter_resolves_to_its_own_provider_pointed_at_openrouter(monkeypatch):
    """OpenRouter speaks OpenAI's wire format, so it reuses that adapter -- but
    it must stay a distinct provider with its own name and base URL, or an
    agent configured for OpenRouter would silently bill an OpenAI key."""
    base = get_settings()
    keyed = base.model_copy(update={"openrouter_api_key": "test-key"})
    monkeypatch.setattr("app.llm.registry.get_settings", lambda: keyed)
    reset_providers()

    provider = get_provider("openrouter")

    assert provider.name == "openrouter"
    assert "openrouter.ai" in str(provider._client.base_url)  # noqa: SLF001


def test_the_openrouter_default_model_is_a_free_one():
    """The point of adding this provider was the free tier. A default that
    is not `:free` spends real money on an agent created with no model set."""
    from app.llm.registry import DEFAULT_MODELS

    assert DEFAULT_MODELS["openrouter"].endswith(":free")


def test_every_known_provider_has_a_default_model():
    from app.llm.registry import DEFAULT_MODELS

    assert set(DEFAULT_MODELS) == set(KNOWN_PROVIDERS)


def _settings_with(**overrides):
    """Settings the registry will actually read, with every provider key
    cleared first. Tests that assert on "is this key set" are worthless if
    the answer depends on the developer's own .env."""
    base = get_settings()
    return base.model_copy(
        update={
            "openai_api_key": None,
            "anthropic_api_key": None,
            "openrouter_api_key": None,
            **overrides,
        }
    )


def test_every_known_provider_needs_a_key(monkeypatch):
    """No provider answers offline any more, so none is configured by default."""
    from app.llm.registry import provider_is_configured

    monkeypatch.setattr("app.llm.registry.get_settings", lambda: _settings_with())

    assert {name: provider_is_configured(name) for name in KNOWN_PROVIDERS} == {
        "openai": False,
        "anthropic": False,
        "openrouter": False,
    }


def test_a_provider_with_its_key_is_configured(monkeypatch):
    from app.llm.registry import provider_is_configured

    monkeypatch.setattr(
        "app.llm.registry.get_settings",
        lambda: _settings_with(anthropic_api_key="test-key"),
    )

    assert provider_is_configured("anthropic") is True
    # Only the one whose key is set -- a single key must not light up the rest.
    assert provider_is_configured("openai") is False


def test_configured_check_rejects_an_unknown_provider():
    from app.llm.registry import provider_is_configured

    with pytest.raises(ValidationError):
        provider_is_configured("definitely-not-a-provider")
```

Append to `apps/api/tests/unit/test_config.py`:

```python
def test_openrouter_is_the_default_provider(monkeypatch):
    monkeypatch.delenv("DEFAULT_LLM_PROVIDER", raising=False)
    assert _settings().default_llm_provider == "openrouter"


def test_a_stale_fake_default_provider_fails_at_startup():
    """`.env.example` used to say DEFAULT_LLM_PROVIDER=fake. A copy of it must
    fail loudly here rather than start and fail on the first LLM call."""
    with pytest.raises(ValidationError, match="default_llm_provider"):
        _settings(default_llm_provider="fake")


def test_the_default_provider_setting_lists_exactly_the_known_providers():
    """Config cannot import the registry (the registry imports config), so the
    two lists are pinned together here instead."""
    from typing import get_args

    from app.core.config import Settings
    from app.llm.registry import KNOWN_PROVIDERS

    annotation = Settings.model_fields["default_llm_provider"].annotation
    assert get_args(annotation) == KNOWN_PROVIDERS


def test_the_suite_never_sees_a_real_provider_key():
    """`tests/conftest.py` overrides the keys a developer's `.env` may hold, so
    no test can spend real requests."""
    settings = get_settings()
    assert settings.openrouter_api_key == "test-openrouter-key"
    assert not settings.openai_api_key
    assert not settings.anthropic_api_key
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_registry.py tests/unit/test_config.py -q`
Expected: FAIL on `test_the_known_providers_are_the_three_real_ones`, `test_fake_is_no_longer_a_provider`, `test_every_known_provider_has_a_default_model`, `test_every_known_provider_needs_a_key`, `test_openrouter_is_the_default_provider`, `test_a_stale_fake_default_provider_fails_at_startup`, `test_the_default_provider_setting_lists_exactly_the_known_providers`, and `test_the_suite_never_sees_a_real_provider_key`.

- [ ] **Step 3: Move `FakeProvider` into the tests**

```bash
cd /c/code/saas-ai/apps/api
git rm -q tests/llm_doubles.py
git mv app/llm/fake_provider.py tests/llm_doubles.py
```

Then edit `apps/api/tests/llm_doubles.py`:

1. Insert at the very top of the file (before `from collections import deque`):

```python
"""LLM provider test doubles shared by the unit and integration suites.

The app ships no offline provider: every provider it resolves calls a real API.
These stand in for one, with no network and no key. `tests/conftest.py`
registers a `FakeProvider` as `openrouter` for every test.
"""

```

2. Replace the class docstring and the `name` attribute:

```python
class FakeProvider:
    """A deterministic provider with no network calls.

    This is production code, not a test fixture, for two reasons. Every test in this
    phase runs against it, so the suite is fast, free and reproducible for anyone who
    clones the repo without an API key — a suite that only runs for people with billing
    configured stops being run. And it is what `ENVIRONMENT=local` falls back to when no
    key is set, so the playground works out of the box.
    """

    name = "fake"
```

with:

```python
class FakeProvider:
    """A deterministic provider with no network calls.

    Every test runs against it, so the suite is fast, free and reproducible for
    anyone who clones the repo without an API key; a suite that only runs for
    people with billing configured stops being run. `name` defaults to
    `openrouter`, the provider it stands in for.
    """
```

3. Add a `name` parameter. Change the `__init__` signature's last parameter line from `fail_with: Exception | None = None,` to:

```python
        fail_with: Exception | None = None,
        name: str = "openrouter",
```

and add as the first line of the `__init__` body:

```python
        self.name = name
```

4. Append to the end of the file:

```python
#: The model `AnsweredByAnotherModel` reports in place of the requested one.
FALLBACK_MODEL = "fallback/m"


class AnsweredByAnotherModel(FakeProvider):
    """Reports a different model on its events than the request asked for,
    as `OpenRouterProvider` does after falling back from a rejected one."""

    async def _stream(self, request: CompletionRequest) -> AsyncIterator[StreamEvent]:
        async for event in super()._stream(request.model_copy(update={"model": FALLBACK_MODEL})):
            yield event
```

- [ ] **Step 4: Remove `fake` from the registry, catalog, pricing and config**

Replace `apps/api/app/llm/registry.py` from its first line through the end of `_build` with:

```python
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.llm.base import LLMProvider
from app.llm.errors import LLMConfigurationError

# Eager, unlike the provider imports in `_build`: this module holds no SDK, only
# the OpenRouter model list and the ids that go with it.
from app.llm.openrouter_models import FALLBACK_MODELS

_PROVIDERS: dict[str, LLMProvider] = {}

# Public, because it is the single source of truth for "is this a provider
# name we recognise" -- `app.agents.schemas` validates against it so an
# unknown string is rejected at the edge (as `invalid_input`) instead of
# reaching `create_agent` and being silently paired with an OpenAI model.
# `Settings.default_llm_provider` repeats these names, because config cannot
# import this module; `tests/unit/test_config.py` pins the two together.
KNOWN_PROVIDERS = ("openai", "anthropic", "openrouter")

# Per PHASE-2.md §2.4. Used to resolve an agent's model when the caller
# names a provider but not a model: each provider's own idiomatic default,
# not a single hardcoded literal that would otherwise pair (e.g.) the
# `anthropic` provider with an OpenAI model string.
DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-opus-5",
    # OpenRouter's reason for existing here is its free tier, so the default
    # must be a `:free` id -- an agent created without an explicit model must
    # not start spending. Taken from `FALLBACK_MODELS` rather than repeated as
    # a literal: both are "an id verified to answer", both are retired without
    # notice, and two copies means retiring one leaves the other pointing at a
    # dead endpoint. That module documents how the list was verified.
    "openrouter": FALLBACK_MODELS[0].id,
}


# Which `Settings` field holds each provider's API key. Defined once, and read
# by both `provider_is_configured` and `_build` below: the dashboard greys out
# a provider on exactly the condition that makes `_build` raise
# `LLMConfigurationError`, and two copies of that mapping would let the two
# answers drift apart.
_API_KEY_FIELDS: dict[str, str] = {
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "openrouter": "openrouter_api_key",
}


def provider_is_configured(name: str) -> bool:
    """Whether this provider has the API key it needs to answer.

    Public because the dashboard asks it before offering a provider: picking
    one with no key produces an agent that cannot answer, and the failure
    surfaces at chat time rather than at the dropdown where it was caused.
    """
    require_known_provider(name)
    return bool(getattr(get_settings(), _API_KEY_FIELDS[name]))


def require_known_provider(name: str) -> None:
    """Reject a provider name nothing here can serve.

    Public because `app/llm/catalog.py` asks the same question of the same
    tuple: one wording of the error -- which the integration tests assert on
    literally -- and one place to change when a provider is added.
    """
    if name not in KNOWN_PROVIDERS:
        raise ValidationError(
            f"unknown provider '{name}'; expected one of {', '.join(KNOWN_PROVIDERS)}"
        )


def get_provider(name: str) -> LLMProvider:
    """Resolve a provider by name, constructing it at most once.

    Cached because each real provider holds an SDK client with its own connection
    pool; building one per request would leak sockets under load.
    """
    require_known_provider(name)

    cached = _PROVIDERS.get(name)
    if cached is not None:
        return cached

    provider = _build(name)
    _PROVIDERS[name] = provider
    return provider


def _build(name: str) -> LLMProvider:
    settings = get_settings()
    if name == "openai":
        if not settings.openai_api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is not set")
        # Imported lazily so this module imports cleanly without every
        # provider SDK, and a missing optional SDK breaks only its own provider.
        from app.llm.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key=settings.openai_api_key)

    if name == "openrouter":
        if not settings.openrouter_api_key:
            raise LLMConfigurationError("OPENROUTER_API_KEY is not set")
        # Lazy for the same reason as the branch above.
        from app.llm.openrouter_provider import OpenRouterProvider

        return OpenRouterProvider(api_key=settings.openrouter_api_key)

    if not settings.anthropic_api_key:
        raise LLMConfigurationError("ANTHROPIC_API_KEY is not set")
    # Lazy for the same reason as the branches above.
    from app.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key=settings.anthropic_api_key)
```

Keep `reset_providers()` at the end of the file unchanged.

In `apps/api/app/llm/catalog.py`, delete the line `"fake": (ModelOption("fake-1", "Fake (offline)", 8_000)),`.

In `apps/api/app/llm/pricing.py`, delete these two lines:

```python
    # Local/testing
    "fake-1": ModelPrice(Decimal("0"), Decimal("0")),
```

In `apps/api/app/core/config.py`, replace:

```python
    # `fake` keeps the playground working with no key configured. Set to
    # `openai` or `anthropic` once a key is present.
    default_llm_provider: str = "fake"
```

with:

```python
    # The provider the dev seed's demo agent and the supplier quote pipeline
    # use; agents a user creates always name their own. OpenRouter's `:free`
    # models need a key but no credits. The names repeat
    # `app.llm.registry.KNOWN_PROVIDERS`, which this module cannot import.
    default_llm_provider: Literal["openai", "anthropic", "openrouter"] = "openrouter"
```

- [ ] **Step 5: Rewrite the app comments that describe `fake` as a provider**

Make these exact replacements.

`apps/api/app/agents/runner.py`: `a provider (the fake included, for tool-only` → `a provider (the test double included, for tool-only`.

`apps/api/app/agents/schemas.py`: `` `Settings.default_llm_provider` (`fake` out of the box) and `model` from `` → `` `Settings.default_llm_provider` (then an offline provider) and `model` from ``.

`apps/api/app/agents/service.py`, replace:

```python
        # created and silently put them all on `fake`. The choice belongs to
        # whoever is creating the agent; `DEFAULT_LLM_PROVIDER` now has exactly
        # one consumer, the dev seed in `app.db.seed`, which asks for it by name.
```

with:

```python
        # created and silently put them all on the offline provider the app
        # then shipped. The choice belongs to whoever is creating the agent;
        # `DEFAULT_LLM_PROVIDER` is read only by the dev seed in `app.db.seed`
        # and by the supplier quote pipeline.
```

`apps/api/app/api/chat.py` module docstring: `the request schema, the fake-provider test seam, the rate` → `the request schema, the provider test seam, the rate`.

`apps/api/app/db/models/agent.py`, replace:

```python
    # (resolved from the caller's validated choice or `DEFAULT_LLM_PROVIDER`,
    # which is `fake` out of the box). A Python-side default of "openai" here
```

with:

```python
    # (the caller's validated choice, or `DEFAULT_LLM_PROVIDER` for the dev
    # seed). A Python-side default of "openai" here
```

`apps/api/app/db/seed.py`, in `demo_agent_input`'s docstring replace:

```python
    and because it is the one place `DEFAULT_LLM_PROVIDER` is still read.
    `CreateAgentInput` requires a provider and a model — deliberately, so a
    user creating an agent in the dashboard has to choose one instead of
    silently inheriting this — and the seed is the caller that genuinely wants
    the configured default: a fresh clone has no API key, so the demo agent has
    to be one that answers offline.

    The model comes from the chosen provider's own entry in `DEFAULT_MODELS`,
    never a literal: pairing `fake` with an OpenAI model id would seed a demo
    agent that cannot answer.
```

with:

```python
    and because it reads `DEFAULT_LLM_PROVIDER`. `CreateAgentInput` requires a
    provider and a model — deliberately, so a user creating an agent in the
    dashboard has to choose one instead of silently inheriting this — and the
    seed is the caller that genuinely wants the configured default: OpenRouter
    unless configured otherwise, which answers once its key is set.

    The model comes from the chosen provider's own entry in `DEFAULT_MODELS`,
    never a literal: pairing `anthropic` with an OpenAI model id would seed a
    demo agent that cannot answer.
```

`apps/api/app/graphql/types.py`, replace:

```python
    anyone made landed on `fake`, the offline provider that answers with a
    canned reply, and they had to find that out and fix it afterwards.
```

with:

```python
    anyone made landed on the offline provider the app then shipped, which
    answered with a canned reply, and they had to find that out and fix it
    afterwards.
```

- [ ] **Step 6: Register the double for every test**

In `apps/api/tests/conftest.py`, add after the existing `os.environ.setdefault("JWT_SECRET", ...)` line:

```python
# Forced, not defaulted: the repo-root `.env` may hold real provider keys, and
# no test may spend real requests. OpenRouter gets a dummy key so it counts as
# configured; `_offline_openrouter` below serves its requests.
os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
os.environ["OPENAI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["DEFAULT_LLM_PROVIDER"] = "openrouter"
```

Change `from collections.abc import AsyncIterator` to `from collections.abc import AsyncIterator, Iterator`, and add after the `anyio_backend` fixture:

```python
@pytest.fixture(autouse=True)
def _offline_openrouter() -> Iterator[None]:
    """Serve `get_provider("openrouter")` from the `FakeProvider` test double.

    Placed in the registry's cache, so the registry itself needs no test hook.
    A test that clears the cache (`reset_providers()`) exercises the real build
    path, against the dummy key above.
    """
    from app.llm import registry
    from tests.llm_doubles import FakeProvider

    registry.reset_providers()
    registry._PROVIDERS["openrouter"] = FakeProvider(name="openrouter")
    yield
    registry.reset_providers()
```

- [ ] **Step 7: Switch the test modules from `fake` to `openrouter`**

Replace `apps/api/tests/factories.py` from the module docstring through the `FAKE_MODEL` line with:

```python
"""Input builders shared by the test suite.

`CreateAgentInput` requires `provider` and `model` — the API deliberately has
no default, so that a user creating an agent through the dashboard has to
choose one rather than silently landing on whatever `DEFAULT_LLM_PROVIDER`
happens to be. That is right for the product and tedious for tests, almost
none of which care which provider an agent is on.

This fills them in with `openrouter`, which `tests/conftest.py` serves from the
`FakeProvider` test double. A test that *does* care passes its own.
"""

from app.agents.schemas import CreateAgentInput

# A fixed pair, not `DEFAULT_MODELS["openrouter"]`: a test fixture that follows
# the registry would change silently with it. The `:free` suffix prices it at
# zero, so cost assertions see 0 rather than NULL.
TEST_PROVIDER = "openrouter"
TEST_MODEL = "test/model:free"
```

and in `agent_input` change `FAKE_PROVIDER` → `TEST_PROVIDER`, `FAKE_MODEL` → `TEST_MODEL`, and the docstring `on the offline provider unless told otherwise` → `on the test provider unless told otherwise`.

Then rewrite the remaining imports, provider names and model ids mechanically:

```bash
cd /c/code/saas-ai/apps/api
files=$(grep -rlE "app\.llm\.fake_provider|[\"']fake(-[12])?[\"']" tests --include=*.py \
  | grep -v -e "tests/unit/test_registry.py" -e "tests/llm_doubles.py")
echo "$files"
sed -i -E \
  -e 's/from app\.llm\.fake_provider import/from tests.llm_doubles import/' \
  -e "s/\"fake-1\"/\"test\/model:free\"/g" -e "s/'fake-1'/'test\/model:free'/g" \
  -e "s/\"fake-2\"/\"test\/other:free\"/g" \
  -e "s/\"fake\"/\"openrouter\"/g" -e "s/'fake'/'openrouter'/g" \
  $files
grep -rn "from tests.llm_doubles import FakeProvider$" tests/llm_doubles.py || true
```

The last command must print nothing (the double must not import itself).

- [ ] **Step 8: Fix the comments and the one test the sed cannot**

`tests/integration/test_graphql.py`: delete the whole `test_configured_providers_reports_fake_as_configured` function (after the sed it reads `by_id["openrouter"]`; `test_configured_providers_reflects_which_keys_are_set` already covers `configured`). Then replace:

```python
    that it rejects the providers that do exist -- `fake` included, which is
    what a fresh clone's agents are actually created with."""
```

with:

```python
    that it rejects the providers that do exist."""
```

and replace:

```python
    web app and only fails at runtime, which is how every agent ended up on
    `fake` in the first place."""
```

with:

```python
    web app and only fails at runtime, which is how every agent once ended up
    on the offline provider the app shipped."""
```

The Step 7 sed already rewrote quoted names inside docstrings (`provider="fake"` became `provider="openrouter"`), so the old text below is the post-sed text.

`tests/integration/test_chat_service.py`: `    (the local default, no API key needed) must be what `send()` resolves via` → `    (served by the test double in `tests/conftest.py`) must be what `send()` resolves via`.

`tests/integration/test_chat_endpoint.py`: `The agent is on `fake`, which always answers -- a 200` → ``The agent is on `openrouter`, which the test double answers -- a 200``.

`tests/integration/test_evaluation_runner.py`: `` # `fake-1` is priced at zero -- priced, so not null. `` → `` # `test/model:free` is priced at zero -- priced, so not null. ``, and `    script, the same one the offline playground shows."""` → `    script: the test double `tests/conftest.py` registers."""`.

`tests/unit/test_agent_schemas.py` module docstring: `— `fake` out of the box — so every agent` → `— an offline provider then — so every agent`.

`tests/unit/test_seed.py`: `pairing the `fake` provider with an OpenAI` → `pairing the `anthropic` provider with an OpenAI`.

- [ ] **Step 9: Lint**

```bash
cd /c/code/saas-ai/apps/api && uv run ruff check --fix app tests && uv run ruff format app tests && uv run mypy app/
```

Expected: `ruff check` fixes import order in the edited test modules and then reports no errors; mypy reports no errors.

- [ ] **Step 10: Run the full API suite**

Run (with the Test Environment exports): `uv run pytest -q`
Expected: all pass. A failure that names `fake` means a test module the sed missed: apply the Step 7 rule to it by hand.

- [ ] **Step 11: Confirm the app no longer names `fake`**

```bash
cd /c/code/saas-ai && grep -rnE "[\"'\`]fake[\"'\`]|fake-1|fake_provider" apps/api/app || echo "clean"
```

Expected: `clean`.

- [ ] **Step 12: Commit**

Run `git status --short` first. Every listed path must be one this task changed: the files named in this task, plus the sed list Step 7 printed. If another path appears (another session's uncommitted work), stage this task's paths by name instead of with `-A`.

```bash
cd /c/code/saas-ai && git add -A apps/api/app apps/api/tests
git commit -m "$(cat <<'EOF'
feat: make OpenRouter the default provider and drop the fake one

The app no longer ships an offline provider. FakeProvider moves to
tests/llm_doubles.py, and an autouse fixture serves it as "openrouter" so
the suite stays offline. DEFAULT_LLM_PROVIDER defaults to openrouter and
rejects any other name at startup, so a stale "fake" fails loudly.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Migrate stored `fake` agents

**Files:**
- Create: `apps/api/alembic/versions/0022_agents_off_fake.py`
- Test: `apps/api/tests/integration/test_agents_off_fake_migration.py` (create)

**Interfaces:**
- Consumes: revision `0021_skip_unsure_reviews`.
- Produces: revision `0022_agents_off_fake`; module constant `MOVE_FAKE_AGENTS: str`.

- [ ] **Step 1: Check the latest revision**

```bash
cd /c/code/saas-ai/apps/api && ls alembic/versions | sort | tail -3
```

If a `0022_*` already exists (another session commits here), number this one `0023` and set `down_revision` to that file's `revision`. The steps below assume `0022` follows `0021_skip_unsure_reviews`.

- [ ] **Step 2: Write the failing test**

Create `apps/api/tests/integration/test_agents_off_fake_migration.py`:

```python
"""Migration 0022 moves agents off the removed `fake` provider.

Its UPDATE runs here against the real table inside a transaction that is rolled
back, so other rows in the shared dev database are left as they were.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.tenancy import TenantContext

pytestmark = pytest.mark.anyio

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0022_agents_off_fake.py"
)


def _migration_statement() -> str:
    spec = importlib.util.spec_from_file_location("migration_0022", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.MOVE_FAKE_AGENTS)


async def _agent(
    owner_connection: Any, tenant: TenantContext, slug: str, provider: str, model: str
) -> uuid.UUID:
    agent_id = uuid7()
    await owner_connection.execute(
        text(
            "INSERT INTO agents "
            "(id, organization_id, name, slug, status, provider, model, "
            " temperature, max_tokens, public_key) "
            "VALUES (:id, :org, :name, :slug, 'draft', :provider, :model, 0.3, 1024, :key)"
        ),
        {
            "id": agent_id,
            "org": tenant.organization_id,
            "name": slug,
            "slug": slug,
            "provider": provider,
            "model": model,
            "key": f"pk_test_{agent_id.hex[:24]}",
        },
    )
    return agent_id


async def test_fake_agents_move_to_the_default_openrouter_model(
    owner_connection: Any, tenant_a: TenantContext
) -> None:
    offline = await _agent(owner_connection, tenant_a, "offline-bot", "fake", "fake-1")
    openai = await _agent(owner_connection, tenant_a, "openai-bot", "openai", "gpt-4o-mini")
    await owner_connection.commit()
    try:
        await owner_connection.execute(text(_migration_statement()))
        result = await owner_connection.execute(
            text("SELECT id, provider, model FROM agents WHERE id IN (:a, :b)"),
            {"a": offline, "b": openai},
        )
        after = {row[0]: (row[1], row[2]) for row in result.all()}
    finally:
        await owner_connection.rollback()

    assert after[offline] == ("openrouter", "google/gemma-4-31b-it:free")
    assert after[openai] == ("openai", "gpt-4o-mini")
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/integration/test_agents_off_fake_migration.py -q`
Expected: FAIL with `FileNotFoundError` for `0022_agents_off_fake.py`.

- [ ] **Step 4: Write the migration**

Create `apps/api/alembic/versions/0022_agents_off_fake.py`:

```python
"""move agents off the fake provider

Revision ID: 0022_agents_off_fake
Revises: 0021_skip_unsure_reviews

The offline `fake` provider is gone from the app, so an agent still on it would
fail every chat with an unknown-provider error. This moves those agents to
OpenRouter's default free model. The model id is frozen here at the value
`DEFAULT_MODELS["openrouter"]` had when this shipped, not read from the app, so
this migration keeps its meaning when that default changes.

Messages, usage events and evaluation runs that recorded `fake` are history and
are left alone.
"""

from alembic import op

revision = "0022_agents_off_fake"
down_revision = "0021_skip_unsure_reviews"
branch_labels = None
depends_on = None

MOVE_FAKE_AGENTS = """
UPDATE agents
SET provider = 'openrouter',
    model = 'google/gemma-4-31b-it:free',
    updated_at = now()
WHERE provider = 'fake'
"""


def upgrade() -> None:
    op.execute(MOVE_FAKE_AGENTS)


def downgrade() -> None:
    # Nothing to restore: once moved, nothing records which agents were on
    # `fake`, and the app can no longer serve that provider anyway.
    pass
```

- [ ] **Step 5: Run the test, then apply the migration**

Run: `uv run pytest tests/integration/test_agents_off_fake_migration.py -q`
Expected: PASS.

Then (with the Test Environment exports): `uv run alembic upgrade head`
Expected: `Running upgrade 0021_skip_unsure_reviews -> 0022_agents_off_fake`. This updates the dev database's agents.

- [ ] **Step 6: Lint and commit**

```bash
cd /c/code/saas-ai/apps/api && uv run ruff check alembic tests && uv run ruff format alembic tests
cd /c/code/saas-ai && git add apps/api/alembic/versions/0022_agents_off_fake.py apps/api/tests/integration/test_agents_off_fake_migration.py
git commit -m "$(cat <<'EOF'
feat: move agents stored on the fake provider to OpenRouter

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Remove `fake` from the web app

**Files:**
- Modify: `apps/web/src/lib/providers.ts`, `apps/web/src/lib/setup-checklist.ts`
- Modify (comments): `apps/web/src/components/agents/CreateAgentForm.tsx`, `apps/web/src/app/dashboard/agents/[id]/page.tsx`
- Test: `apps/web/src/lib/providers.test.ts`, `lib/setup-checklist.test.ts`, `components/agents/CreateAgentForm.test.tsx`, `components/evaluations/StartRunForm.test.tsx`, `components/ui/Field.test.tsx`

**Interfaces:**
- Consumes: `configuredProviders { id configured }` (unchanged GraphQL shape).
- Produces: unchanged exports `providerLabel`, `creatableProviders`, `editableProviders`, `deriveChecklist`.

- [ ] **Step 1: Update the tests first**

`apps/web/src/lib/providers.test.ts`:
1. In `ALL`, delete `{ id: "fake", configured: true },`.
2. Delete the test `"says what \`fake\` actually is, since its name explains nothing"`.
3. Delete the test `"never offers \`fake\` on a new agent"`.
4. Change the body of `"is empty when the server holds no keys at all"` to `expect(creatableProviders([{ id: "openai", configured: false }])).toEqual([]);`.
5. Replace the two tests `"keeps \`fake\` when it is the agent's current provider"` and `"drops \`fake\` for an agent already on a real provider"` with:

```ts
  it("keeps a provider the API no longer lists, disabled, for the agent on it", () => {
    // An agent stored before a provider was retired must still render its own
    // provider rather than a blank select.
    expect(editableProviders(ALL, "retired")[0]).toEqual({ id: "retired", configured: false });
  });
```

`apps/web/src/lib/setup-checklist.test.ts`:
1. Delete `{ id: "fake", configured: true },` from both `NO_KEYS` and `OPENAI_KEYED`.
2. In `"reads real-provider from the server's keys, not from the agents"`, replace the comment with:

```ts
    // It used to be read from the agents. Creating an agent now requires
    // choosing a configured provider, so that would be trivially true the
    // moment anyone created their first agent -- a tick for a step they had
    // not done. What it is really asking is whether this install has a key.
```

3. Replace the test `"leaves real-provider undone when only the offline provider is available"` with:

```ts
  it("leaves real-provider undone when no provider has a key", () => {
    expect(stepById([{ status: "ACTIVE" }], NO_KEYS, "real-provider").done).toBe(false);
  });
```

`apps/web/src/components/agents/CreateAgentForm.test.tsx`:
1. In `PROVIDERS`, delete `{ id: "fake", configured: true },`.
2. In `"asks for a provider and a model, not just a name"`, replace the comment's last line `// on \`fake\`.` so the comment reads:

```ts
    // The whole point. The form used to send a name alone, so the API resolved
    // the provider from DEFAULT_LLM_PROVIDER and every agent silently started
    // on the offline provider the app then shipped.
```

3. Delete the test `"never offers the offline provider"`.
4. In `"explains itself instead of rendering an empty dropdown when no key is set"`, change `providers: [{ id: "fake", configured: true }]` to `providers: [{ id: "openai", configured: false }]`.

`apps/web/src/components/evaluations/StartRunForm.test.tsx`: change `AGENTS` to `[{ id: "agent-1", name: "Sales bot", provider: "openrouter", model: "test/model:free" }]`, and in `PROVIDERS` replace `{ id: "fake", configured: true },` with `{ id: "openrouter", configured: true },`.

`apps/web/src/components/ui/Field.test.tsx`: replace both occurrences of ``"`fake` answers offline and costs nothing."`` with ``"OpenRouter's `:free` models cost nothing."``.

- [ ] **Step 2: Run the tests to confirm the fixtures still pass**

Run: `cd /c/code/saas-ai/apps/web && npm test`
Expected: PASS. The helpers already treat every id other than `fake` the way the updated tests expect, so this task is a cleanup under green tests: Steps 3 and 4 delete the `fake` special cases, and Step 5 must stay green.

- [ ] **Step 3: Update `providers.ts` and `setup-checklist.ts`**

`apps/web/src/lib/providers.ts`:
1. Change `/** Whether the server holds this provider's API key. \`fake\` is always true. */` to `/** Whether the server holds this provider's API key. */`.
2. Delete:

```ts
/** The offline provider: answers with a canned reply, needs no API key. */
const OFFLINE = "fake";
```

3. Delete `  fake: "Fake (offline)",` from `LABELS`.
4. Replace the `creatableProviders` docstring and body with:

```ts
/**
 * What the *create* form may offer: providers that can actually answer.
 *
 * An empty result is meaningful: the server holds no keys, and the form has to
 * say so rather than render an empty dropdown.
 */
export function creatableProviders(providers: readonly ProviderInfo[]): ProviderInfo[] {
  return providers.filter((provider) => provider.configured);
}
```

5. Replace the `editableProviders` docstring with:

```ts
/**
 * What the *edit* form may offer, given the provider the agent is on now.
 *
 * Wider than `creatableProviders`: unconfigured providers stay in the list so
 * the form can disable them with a visible reason, rather than hiding the
 * option and leaving the user to wonder where it went.
 */
```

6. In `editableProviders`' body, replace:

```ts
  const listed = providers.filter(
    (provider) => provider.id !== OFFLINE || currentProvider === OFFLINE,
  );
  if (listed.some((provider) => provider.id === currentProvider)) return listed;
```

with:

```ts
  if (providers.some((provider) => provider.id === currentProvider)) return [...providers];
```

and the final `return [{ id: currentProvider, configured: false }, ...listed];` with `return [{ id: currentProvider, configured: false }, ...providers];`.

`apps/web/src/lib/setup-checklist.ts`:
1. Replace the `ChecklistAgent` doc comment's middle with:

```ts
/** Only the field the checklist reads, so the Agents query can change shape
 * without touching this module. `provider` used to be read here too, to tell
 * whether any agent had been moved off the offline provider the app once
 * shipped; see `real-provider` below for why that question moved to the server. */
```

2. Delete:

```ts
/** Answers offline with a canned reply, and needs no API key -- so it is
 * always reported as `configured` and can never satisfy the step below. */
const OFFLINE_PROVIDER = "fake";
```

3. Replace the `real-provider` step's comment and `done` with:

```ts
      // Asked of the server, not of the agents: creating an agent requires
      // choosing a configured provider, so "some agent exists" would tick the
      // moment the first agent did. An empty list (the query has not resolved)
      // is correctly false rather than true.
      done: providers.some((provider) => provider.configured),
```

- [ ] **Step 4: Rewrite the two component comments**

`apps/web/src/components/agents/CreateAgentForm.tsx`, replace:

```ts
 * a name, and the API filled the rest in from `DEFAULT_LLM_PROVIDER` — `fake`
 * out of the box — so every agent anyone created started on the offline
 * provider that answers with a canned reply, and they had to discover that and
 * fix it on the agent's detail page.
```

with:

```ts
 * a name, and the API filled the rest in from `DEFAULT_LLM_PROVIDER`, which
 * then pointed at an offline provider that answered with a canned reply — so
 * every agent anyone created started there, and they had to discover that and
 * fix it on the agent's detail page.
```

`apps/web/src/app/dashboard/agents/[id]/page.tsx`: change `fire once for "fake" and` to `fire once for the first provider and`, and replace:

```tsx
                    * guessing, a disabled one says why. `fake` appears only
                    * for an agent already on it — every agent created before
                    * providers became a required choice is, and a select with
                    * no option matching its value renders blank. */}
```

with:

```tsx
                    * guessing, a disabled one says why. The agent's own
                    * provider always appears, even one the API no longer
                    * lists: a select with no option matching its value
                    * renders blank. */}
```

- [ ] **Step 5: Run the web checks**

```bash
cd /c/code/saas-ai/apps/web && npm test && npm run typecheck && npm run lint
cd /c/code/saas-ai && grep -rnE "[\"'\`]fake[\"'\`]|Fake \(offline\)|OFFLINE" apps/web/src || echo "clean"
```

Expected: all tests pass, no type or lint errors, and `clean`.

- [ ] **Step 6: Commit**

```bash
cd /c/code/saas-ai && git add apps/web/src
git commit -m "$(cat <<'EOF'
feat: drop the offline provider from the dashboard

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Configuration files and docs

**Files:**
- Modify: `.env.example`, `docker-compose.yml`, `README.md`, `docs/PHASE-2.md`, `docs/PHASE-9.md`

**Interfaces:** none.

- [ ] **Step 1: `.env.example`**

Replace:

```
# Only the development seed (`make seed`) reads this -- it is the provider its
# demo agent is created on. `fake` answers offline with a canned reply and
# needs no key, so `make seed` works on a fresh clone with nothing configured.
# It is NOT a default for agents you create: those always name their own
# provider, which is why every agent used to silently end up on `fake`.
DEFAULT_LLM_PROVIDER=fake
```

with:

```
# The provider the development seed's demo agent and the supplier quote
# pipeline use: openai, anthropic or openrouter. It needs that provider's key
# above; without one, chat reports a configuration error and supplier emails
# wait for review. It is NOT a default for agents you create: those always
# name their own provider.
DEFAULT_LLM_PROVIDER=openrouter
```

After the line `# GMAIL_POLL_INTERVAL_MINUTES=5`, add:

```
# The supplier pipeline's LLM model (classification, column mapping, product
# matches). Empty means DEFAULT_LLM_PROVIDER's default model, a free OpenRouter
# one; free models share 50 requests a day per account until it holds credits.
# SUPPLIER_QUOTE_LLM_MODEL=
# LLM product-match calls per email, 0 to 25. Rows past it wait for review.
# GMAIL_LLM_MATCHES_PER_MESSAGE=5
```

- [ ] **Step 2: `docker-compose.yml`**

In the `api` service, replace:

```yaml
      # this container -- without these lines every real provider resolves to
      # `LLMConfigurationError` here, and only the offline `fake` provider
      # works. Unset on the host stays unset in the container (no `:-default`),
      # which is what keeps `fake` the out-of-the-box path.
```

with:

```yaml
      # this container -- without these lines every provider resolves to
      # `LLMConfigurationError` here. Unset on the host stays unset in the
      # container (no `:-default`).
```

In both the `api` and `worker` services, change `DEFAULT_LLM_PROVIDER: ${DEFAULT_LLM_PROVIDER:-fake}` to `DEFAULT_LLM_PROVIDER: ${DEFAULT_LLM_PROVIDER:-openrouter}`, and after each `GMAIL_POLL_INTERVAL_MINUTES: ${GMAIL_POLL_INTERVAL_MINUTES:-5}` line add:

```yaml
      SUPPLIER_QUOTE_LLM_MODEL: ${SUPPLIER_QUOTE_LLM_MODEL:-}
      GMAIL_LLM_MATCHES_PER_MESSAGE: ${GMAIL_LLM_MATCHES_PER_MESSAGE:-5}
```

Check: `cd /c/code/saas-ai && docker compose config --quiet && echo ok` prints `ok`.

- [ ] **Step 3: `README.md`**

Replace:

```
and prompts; a real LLM call — a provider abstraction (OpenAI, Anthropic, and a
network-free `fake`), conversations and messages, a streaming `POST /api/v1/chat/stream`
```

with:

```
and prompts; a real LLM call — a provider abstraction (OpenAI, Anthropic, and
OpenRouter as the default), conversations and messages, a streaming `POST /api/v1/chat/stream`
```

and replace `llm/        provider abstraction: openai, anthropic, fake; capabilities, pricing, registry` with `llm/        provider abstraction: openai, anthropic, openrouter; structured output, capabilities, pricing, registry`.

- [ ] **Step 4: `docs/PHASE-2.md`**

1. `LLMProvider  ── openai │ anthropic │ openrouter │ fake` → `LLMProvider  ── openai │ anthropic │ openrouter`.
2. In the §2.4 table, replace the row ``| `fake` | `fake-1` | n/a | echoed back for assertions |`` with ``| `openrouter` | first entry of `FALLBACK_MODELS` (a `:free` id) | disabled | `temperature` sent |``.
3. Replace the whole §2.5 (from `### 2.5 The fake provider is a first-class citizen` through `playground works out of the box.`) with:

```
### 2.5 Tests run against a test double, not a provider

`FakeProvider` lives in `apps/api/tests/llm_doubles.py`. It replays a scripted sequence of
events deterministically, with no network, and an autouse fixture registers it as
`openrouter` in the provider registry for every test.

That keeps the suite fast, deterministic and free for anyone who clones the repo without an
API key. A test suite that only runs for people with billing set up is a test suite that
stops being run.

The app itself has no offline provider. It used to ship `FakeProvider` as one and fall back
to it when no key was set; it now defaults to OpenRouter, whose `:free` models need a key
but no credits. Without a key, chat reports a configuration error instead of a canned reply.
```

4. In the risks table, replace ``| No API key in this environment | `FakeProvider` covers the whole pipeline; real providers are unit-tested against mocked SDK clients. Live verification needs the user's key. |`` with ``| No API key in this environment | Tests run against the `FakeProvider` test double; real providers are unit-tested against mocked SDK clients. Live verification needs the user's key. |``.

- [ ] **Step 5: `docs/PHASE-9.md`**

Replace:

```
   review* so the owner can add that address. Otherwise it is ignored, and so is every
   unknown sender when the provider has no structured output or fails. An ignored email is
   not re-checked later.
```

with:

```
   review* so the owner can add that address. Otherwise it is ignored, and so is every
   unknown sender when the LLM call fails (no `OPENROUTER_API_KEY`, the free tier's daily
   limit, or a reply that is not one valid JSON object). An ignored email is not re-checked
   later. Sender rules need no LLM call, so listing a supplier's addresses also saves
   requests.
```

and replace:

```
   with that SKU matches without review. Each message makes at most 25 LLM match calls;
   rows beyond that wait for review.
```

with:

```
   with that SKU matches without review. Each message makes at most
   `GMAIL_LLM_MATCHES_PER_MESSAGE` LLM match calls (5 by default, up to 25); rows beyond
   that wait for review. The pipeline's LLM calls use `SUPPLIER_QUOTE_LLM_MODEL` when set,
   else the default provider's default model, a free OpenRouter one.
```

- [ ] **Step 6: Commit**

```bash
cd /c/code/saas-ai && git add .env.example docker-compose.yml README.md docs/PHASE-2.md docs/PHASE-9.md
git commit -m "$(cat <<'EOF'
docs: describe OpenRouter as the default and the supplier LLM settings

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Final verification

**Files:** none changed unless a check fails.

- [ ] **Step 1: Full API checks**

```bash
cd /c/code/saas-ai/apps/api && \
export DATABASE_URL="postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai" \
       MIGRATION_DATABASE_URL="postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai" \
       REDIS_URL="redis://localhost:6379/0" && \
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app/
```

Expected: all tests pass; ruff and mypy report nothing.

- [ ] **Step 2: Full web checks**

```bash
cd /c/code/saas-ai/apps/web && npm test && npm run typecheck && npm run lint
```

Expected: all pass.

- [ ] **Step 3: Success criteria greps**

```bash
cd /c/code/saas-ai && grep -rnE "[\"'\`]fake[\"'\`]|fake-1|fake_provider|Fake \(offline\)" apps/api/app apps/web/src || echo "app clean"
git grep -n "class FakeProvider" -- apps/api
```

Expected: `app clean`, and exactly one `class FakeProvider` in `apps/api/tests/llm_doubles.py`.

- [ ] **Step 4: Live check against OpenRouter (ask the user first)**

This spends one of the account's free daily requests with the real key in the repo-root `.env`. Only with the user's go-ahead:

```bash
cd /c/code/saas-ai/apps/api && uv run python -c "
import asyncio
from pydantic import BaseModel
from app.llm.registry import DEFAULT_MODELS, get_provider
from app.llm.types import CompletionRequest, Message

class Answer(BaseModel):
    is_supplier: bool

async def main():
    provider = get_provider('openrouter')
    request = CompletionRequest(
        model=DEFAULT_MODELS['openrouter'],
        messages=[Message.text('user', 'Sender: quotes@acme.example. Subject: Price list October.')],
        system='Decide whether this email comes from a supplier.',
        max_tokens=200,
    )
    print(await provider.generate_structured(request, Answer))

asyncio.run(main())
"
```

Expected: a line like `is_supplier=True`. An `LLMStructuredOutputError` means the default free model does not follow JSON instructions; report the model id to the user rather than changing code.

# OpenRouter as the Default LLM Provider

**Status:** Draft for user review  
**Date:** 2026-10-09  
**Builds on:** `2026-10-08-gmail-supplier-quote-analysis-design.md`, `docs/PHASE-2.md`

## 1. Goal

Remove the offline `fake` LLM provider from the running application and make
OpenRouter the default provider. Make OpenRouter able to return structured
output, so the supplier quote pipeline's AI steps produce real suggestions for
the first time.

This is part B of a two-part change. Part A (header aliases and a layered
column-mapping flow for supplier workbooks) gets its own spec and builds on this
one.

**Success criteria**

- No module under `apps/api/app` or `apps/web/src` treats `fake` as a provider.
  `FakeProvider` exists only under `apps/api/tests`.
- A fresh clone with `OPENROUTER_API_KEY` set gets a demo agent that answers
  through OpenRouter, with no other LLM setting changed.
- With `OPENROUTER_API_KEY` set, a supplier workbook with no confirmed mapping
  receives an AI-proposed mapping instead of an empty one.
- With no key set, the API still starts. Chat reports a configuration error and
  supplier emails go to review.
- The API and web test suites pass offline.

## 2. Existing context

- `app/llm/registry.py` lists `fake`, `openai`, `anthropic` and `openrouter`.
  `DEFAULT_LLM_PROVIDER` defaults to `fake`, and `fake` also appears in the
  model catalog, the pricing table, the seed, `.env.example`, `docker-compose.yml`
  and the web provider helpers.
- Tests use `FakeProvider` in about 136 places. Some inject it directly, for
  example through the `get_chat_provider` dependency. Others create agents with
  `provider="fake"` and rely on the registry to build one.
- **No real provider implements `generate_structured`.** `OpenAIProvider`
  (which `OpenRouterProvider` extends) and `AnthropicProvider` both raise
  `NotImplementedError`, and `FakeProvider` returns an empty schema instance.
  `app/supplier_quotes/classification.py` catches the failure and falls back to
  an empty result. So in every configuration, sender classification, mapping
  suggestion and product matching have always returned nothing, and every new
  workbook has needed a fully manual mapping.
- `app/evaluations/judge.py` already gets JSON from real models by calling
  `generate()` with a JSON-only instruction, stripping a code fence and
  validating with Pydantic.
- OpenRouter `:free` models allow 50 requests per day per account until it holds
  credits. One supplier email can currently make up to 27 calls: one
  classification, one mapping suggestion and up to 25 product matches
  (`_LLM_MATCHES_PER_MESSAGE` in `app/gmail/sync.py`).

## 3. Decisions

| Decision | Choice | Reason |
| --- | --- | --- |
| Fate of `FakeProvider` | Move to `apps/api/tests/llm_doubles.py` | Tests stay offline, free and deterministic; production code has no fake. |
| Default provider | `openrouter` with `DEFAULT_MODELS["openrouter"]` | Free tier needs no billing setup. |
| Existing `fake` agents | Migrate to `openrouter` and the default free model | A `fake` agent would otherwise fail with an unknown-provider error. |
| History rows that say `fake` | Leave unchanged | Messages, usage and evaluation runs record what happened. |
| Structured output method | `generate()` with a JSON-only prompt, then Pydantic | Works on any model, including free models without `response_format` support; proven by the judge. |
| Missing API key | Start normally; fail the call | Matches how OpenAI and Anthropic already behave without keys. |

Rejected alternatives for structured output: OpenRouter `response_format` with a
JSON schema (only some models support it, and the free roster changes without
notice), and forcing a single tool call (many free models call tools poorly,
and it is more machinery than JSON needs).

## 4. Removing the fake provider

### 4.1 API

- `app/llm/fake_provider.py` moves into `tests/llm_doubles.py`, next to
  `AnsweredByAnotherModel`. Its `name` becomes a constructor argument so a test
  can register it under a real provider name.
- `app/llm/registry.py`: `KNOWN_PROVIDERS = ("openai", "anthropic", "openrouter")`.
  The `fake` branch, `DEFAULT_MODELS["fake"]` and the "or use the 'fake'
  provider" wording in configuration errors are removed. Every known provider
  now has an API-key field, so `provider_is_configured` loses its no-key case.
- `app/llm/catalog.py` and `app/llm/pricing.py` drop `fake` and `fake-1`.
- `app/core/config.py`: `default_llm_provider` defaults to `openrouter`.
- `app/supplier_quotes/classification.py` resolves its model from the
  configured provider (section 6) and no longer falls back to
  `DEFAULT_MODELS["fake"]`.
- `app/db/seed.py` is unchanged in logic. It already reads
  `DEFAULT_LLM_PROVIDER`, so the demo agent lands on OpenRouter.
- Comments that describe `fake` as a runtime provider are rewritten, for
  example in `app/agents/`, `app/api/chat.py`, `app/db/models/agent.py` and
  `app/graphql/types.py`.

### 4.2 Database

Migration `0022` updates `agents` rows whose `provider` is `fake` to
`openrouter`, with `model` set to `DEFAULT_MODELS["openrouter"]` as of the
migration (written as a literal in the migration, since migrations must not
change when the app's constant does). Downgrade is a no-op: once migrated,
nothing records which agents were `fake`.

An evaluation run still queued on `fake` when the migration runs fails with an
unknown-provider error. That is accepted; such runs only exist in development
databases.

### 4.3 Behaviour without `OPENROUTER_API_KEY`

- The API starts. `get_provider("openrouter")` raises the existing
  `LLMConfigurationError`, which chat already reports as an error event.
- The dashboard already greys out providers whose `configured` is false.
- Supplier classification, mapping suggestion and product matching fall back
  to review, as they do on any provider failure.

### 4.4 Web

- `apps/web/src/lib/providers.ts` drops the `fake` label and the `OFFLINE`
  special cases in `creatableProviders` and `editableProviders`.
  `editableProviders` already keeps an agent's current provider even when the
  API no longer lists it.
- `apps/web/src/lib/setup-checklist.ts`: the "Connect a real model provider"
  step is done when any provider reports `configured`.
- Comments mentioning `fake` in `CreateAgentForm.tsx` and the agent detail page
  are rewritten. Tests are updated to match.

### 4.5 Tests

- An autouse fixture in `tests/conftest.py` places a `FakeProvider` named
  `openrouter` into the registry's provider cache before each test and calls
  `reset_providers()` after it, so `get_provider("openrouter")` returns the
  double without a key or network access and nothing leaks between tests.
  `test_registry.py` calls `reset_providers()` first, so it still exercises the
  real build path. Tests that injected a `FakeProvider` directly keep doing so,
  importing it from `tests.llm_doubles`.
- Tests that create agents or evaluation runs with `provider="fake"` or
  `judge_provider="fake"` switch to `openrouter`. Assertions on the stored
  provider name change to match.
- `tests/unit/test_fake_provider.py` keeps covering the double, importing it
  from its new location.
- `test_registry.py` asserts that `fake` is rejected as an unknown provider.

### 4.6 Configuration and docs

- `.env.example`: `DEFAULT_LLM_PROVIDER=openrouter`, and the comment explains
  that an OpenRouter key is needed for any AI answer.
- `docker-compose.yml`: `${DEFAULT_LLM_PROVIDER:-openrouter}` for the API and
  the worker; the comments about `fake` being the out-of-the-box path are
  rewritten.
- `README.md`, `docs/ARCHITECTURE.md` and the phase docs that describe current
  behaviour (`docs/PHASE-2.md`, `PHASE-3.md`, `PHASE-4.md`, `PHASE-8.md`) stop
  presenting `fake` as a provider. Plans and specs under `docs/superpowers/`
  are historical records and stay unchanged.

## 5. Structured output

New module `app/llm/structured.py`:

```python
async def generate_structured(
    provider: LLMProvider, request: CompletionRequest, schema: type[SchemaT]
) -> SchemaT: ...
```

1. Append an instruction to `request.system`: reply with only one JSON object
   that conforms to the JSON Schema that follows, with no prose and no code
   fence, followed by `schema.model_json_schema()`. The caller's system prompt
   is kept intact before it.
2. Call `provider.generate(request)`. OpenRouter keeps its model fallback and
   disabled reasoning.
3. Strip a leading and trailing code fence, then
   `schema.model_validate_json(text)`.
4. An empty reply, invalid JSON or a schema mismatch raises a new
   `LLMStructuredOutputError` from `app/llm/errors.py`. The log records the
   model and the error type, never the reply text, because it contains
   supplier prices.

`OpenAIProvider.generate_structured` and `AnthropicProvider.generate_structured`
delegate to this function. `OpenRouterProvider` inherits the OpenAI one.

The fence-stripping helper moves from `app/evaluations/judge.py` into
`app/llm/structured.py`, and the judge imports it from there. The judge keeps
calling `generate()` itself because it needs the response's usage.

The validation in `classification.py` stays the gate: proposed labels must
exist in the workbook, the model may not invent a currency or unit, and any
failure still becomes a review state.

## 6. Request budget

The 50-requests-per-day free limit is shared across the whole OpenRouter
account, so supplier processing can use up the playground's requests too.

New settings in `app/core/config.py`:

- `supplier_quote_llm_model: str | None = None`. The model for supplier
  classification, mapping and matching. When unset, it is
  `DEFAULT_MODELS[default_llm_provider]`. Switching to a paid model needs no
  code change.
- `gmail_llm_matches_per_message: int = 5`, between 0 and 25. It replaces the
  hard-coded `_LLM_MATCHES_PER_MESSAGE = 25`. AI product matches always need
  review, so a lower cap costs convenience, not correctness. Rows over the cap
  are recorded as unmatched and need review, as they are today.

A rate-limit response surfaces as `LLMUnavailableError`, which the supplier
pipeline already turns into review without retrying. `docs/PHASE-9.md` notes
that adding a supplier's sender emails or domains skips the classification call.

## 7. Testing

- `tests/unit/test_structured_output.py`, with a stub provider whose
  `generate()` returns fixed text: plain JSON, fenced JSON, prose, a missing
  required field, an empty reply. Also asserts that the caller's system prompt
  survives and the schema is appended.
- `generate_structured` on `OpenRouterProvider` through the existing HTTP stubs
  in `tests/unit/_llm_stubs.py`.
- `test_registry.py`: `fake` is unknown; the default provider is `openrouter`;
  every known provider has a key field.
- `test_config.py`: defaults and bounds of the two new settings.
- Supplier pipeline: `supplier_quote_llm_model` reaches the request, and no more
  than `gmail_llm_matches_per_message` product-match calls are made per email.
- Migration `0022` against the integration database: a `fake` agent becomes
  `openrouter`; other agents are untouched.
- Web: `providers.test.ts` and `setup-checklist.test.ts` without `fake`.
- Both full suites pass offline.

## 8. Out of scope

- Recording supplier-pipeline LLM calls in `usage_events`. They are free on the
  default model. If a paid `supplier_quote_llm_model` is used, cost tracking
  should be added separately.
- The `hashing` embedding provider. It computes real lexical embeddings rather
  than canned answers, and the only alternative is OpenAI embeddings.
- Part A: header aliases and the layered mapping flow.

# Phase 8 — Embeddable Chat Widget: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a business pastes one `<script>` tag into its site and anonymous visitors chat with its agent — streamed, resumable after reload, restricted to the business's domains — while the owner configures it and reads the conversations in the dashboard.

**Architecture:** a `widget_settings` table (RLS) plus one `SECURITY DEFINER` lookup by `Agent.public_key`; signed visitor tokens (`typ="widget"`); a public `/api/v1/widget/*` router that reuses the chat streaming body with a public event projection and Redis limits; a Next.js `/embed/[publicKey]` page whose `frame-ancestors` is set per agent by middleware; a hand-written `public/widget.js` loader; a Website widget card and a Conversations page in the dashboard.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async, Alembic, Strawberry, PyJWT, Redis limiter; Next.js 15 App Router, urql, vitest.

**Spec:** [docs/superpowers/specs/2026-09-25-embeddable-widget-design.md](../specs/2026-09-25-embeddable-widget-design.md). Section references (§n) below point there. Read it first.

## Global Constraints

- Python **3.12**, `uv`; API code under `apps/api/app/`. **GNU `make` is NOT available** — run the underlying commands directly (see the root `Makefile` for them).
- Async tests use **`anyio` only** (`pytestmark = pytest.mark.anyio`); `pytest-asyncio` is deliberately absent.
- The suite runs with `filterwarnings = ["error"]` — any warning fails it.
- **No test may make a network call.** API tests run in-process (`httpx.ASGITransport`); chat tests override `get_chat_provider` with the existing `FakeProvider`.
- **Integration tests need all three overrides in every shell invocation:**
  ```sh
  export DATABASE_URL="postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai"
  export MIGRATION_DATABASE_URL="postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai"
  export REDIS_URL="redis://localhost:6379/0"
  ```
  Start infra with `docker compose up -d --wait db redis`; migrate with `cd apps/api && uv run alembic upgrade head`. **Check no other `pytest` is running before starting one.**
- Tenant-owned tables get `organization_id UUID NOT NULL` + RLS via `enable_rls(op, table)`. Never inline policy SQL.
- **Two-layer tenancy is mandatory:** an explicit `organization_id` predicate *in addition to* RLS, on every query.
- **PostgreSQL FK checks bypass the referencing session's RLS.** Any INSERT establishing an FK (`agent_id`) needs a scoped ownership SELECT first.
- Primary keys are `app.core.ids.uuid7()`.
- **Widget requests never authenticate with a dashboard JWT, and a widget token is never accepted where a dashboard token is.** Enforced by `typ`.
- **Every public widget refusal (unknown key, draft/disabled agent, widget not enabled) is the same 404 `widget not found`.**
- **No IP, `visitor_id`, message text, tool argument or tool result in any log line.** Public-key logs carry only the first 8 characters.
- Visitor message length **1..2000**; token lifetime **30 days**; limits exactly per spec §5 (20/h new sessions per IP, 10/min per visitor, 30/min per IP, `daily_message_cap` per agent per UTC day, 120/min frame-policy per IP).
- `ruff check .`, `ruff format --check .`, `mypy --strict app/` must pass; web: `npx tsc --noEmit`, `npx eslint .`, `npx vitest run`, `npx next build`.
- **`docs/DESIGN.md` is binding for web UI — read it before Task 6, update it after.** No colour outside `globals.css` except the widget's own `brand_color`, which is data.
- Conventional Commits ending with exactly:
  `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`
- **Baseline:** record the exact backend and web test counts on `phase-8-widget` at the start of Task 1 and report them in every task's report.

## Review Focus

1. **A visitor supplying another visitor's `conversation_id`** (guessed or leaked) — 404 on stream and invisible on resume; nothing appended to that conversation. Pinned in Task 3.
2. **The widget turned off, or the agent set to draft/disabled, while a visitor has a valid 30-day token** — the very next session/conversation/stream call is 404. Pinned in Task 3.
3. **A tool-using turn over the widget** — the SSE body contains no `arguments`, no `result`, no `excerpt`, no `cost_usd`, no `model`. Pinned in Task 2 (projection unit) and Task 3 (end-to-end body scan).
4. **An `allowed_origins` entry with a path, wildcard, `http://` on a real host, or a trailing slash** — rejected or normalized before storage, so nothing unexpected ever reaches `frame-ancestors`. Pinned in Task 1 (normalizer) and Task 5 (header builder refuses anything that is not a bare origin even if the API returned it).
5. **The playground path after `_stream_body` moves** — `test_chat_endpoint.py` passes with only import changes. Pinned in Task 2.

## Existing interfaces you build on

- `app/api/chat.py` — `ChatStreamRequest`, `_event_payload(event) -> dict`, `_sse`, `_pump`, `_queue_title`, `_stream_body(events, first_event, session_cm, title_conversation_id, organization_id)`, `get_chat_provider`, the first-event pre-read pattern in `chat_stream`.
- `app/chat/service.py` — `ChatService(session, tenant, provider_override=None, ...)`, `send(agent_id, user_text, conversation_id=None, channel=ConversationChannel.API, override_provider=None, override_model=None, prompt_version_id=None) -> AsyncIterator[ChatEvent]`; event classes `ChatMessageStart(conversation_id, message_id, created)`, `ChatCitations`, `ChatTextDelta`, `ChatToolCallStart`, `ChatToolCallEnd`, `ChatMessageEnd`, `ChatError(code, message)`.
- `app/conversations/service.py` — `ConversationService.create(agent_id, CreateConversationInput(channel, visitor_id))`, `get(id)`, `history(conversation_id, limit)`, `list_for_agent(agent_id, channel, limit, offset)`. `app/conversations/queue.py` — `should_title`, `TITLED_CHANNELS` (stays playground-only).
- `app/leads/service.py` — `LeadService.create(agent_id, conversation_id, data)` (already SELECTs the conversation for tenancy).
- `app/agents/service.py` — `AgentService.get_agent`, `get_config`. `app/db/models/agent.py` — `Agent.public_key`, `AgentStatus`, `AgentConfig.greeting`, `AgentConfig.fallback_message`.
- `app/core/security.py` — `_encode(claims, lifetime)`, `decode_token(token, *, expected_type)`, `TokenPayload`. `app/core/tenancy.py` — `TenantContext`, `tenant_session`, `untenanted_session`. `app/core/rate_limit.py` — `enforce_rate_limit(key, *, limit, window_seconds)`. `app/core/errors.py` — `NotFoundError`, `ValidationError`, `PermissionDeniedError`, `RateLimitError`, `AuthenticationError`.
- `app/api_keys/service.py` + `alembic/versions/0015_api_keys.py` — the pattern for a pre-tenant `SECURITY DEFINER` resolve and for owner/admin checks. **Copy that migration's function/grant block shape.**
- `app/api/auth.py` — `_client_key(request)` for the client IP.
- `app/graphql/*` — `_build`, `_require_tenant`, resolver/service idiom; `tests/integration/test_graphql_api_keys.py` is the closest GraphQL test.
- Migrations head `0015_api_keys`. Yours is `0016_widget_settings`.
- Web: `src/lib/sse.ts` (`toSSEEvent`, `parseSSEStream`, `streamChat`), `src/lib/api.ts` (`API_URL`), `src/lib/auth-proxy.ts` (`proxyTargetUrl`), `src/lib/security-headers.ts`, `src/middleware.ts`, `src/components/chat/ChatMessage.tsx` (`ChatMessage`, `ChatMessageData`), `src/lib/conversation-transcript.ts`, `src/components/agents/McpAccessCard.tsx` (closest card), `src/components/shell/nav.ts`, `src/app/dashboard/agents/[id]/page.tsx`, `src/app/dashboard/leads/page.tsx`.

## File Structure

```text
apps/api/app/
├── db/models/widget.py                         # Task 1 — WidgetSettings, WidgetPosition
├── alembic/versions/0016_widget_settings.py    # Task 1 — table, RLS, resolve_widget()
├── widget/__init__.py
├── widget/origins.py                           # Task 1 — normalize_origin(s), pure
├── widget/schemas.py                           # Task 1 — UpdateWidgetSettingsInput
├── widget/service.py                           # Task 1 — WidgetSettingsService, resolve_public
├── widget/events.py                            # Task 2 — project_public_event, pure
├── api/streaming.py                            # Task 2 — moved: _sse, _pump, _queue_title, stream_body
├── api/chat.py                                 # Task 2 — imports streaming.py
├── chat/service.py                             # Task 2 — send(visitor_id=...)
├── leads/service.py                            # Task 2 — source from channel
├── widget/tokens.py                            # Task 3 — create/decode visitor tokens
├── api/widget.py                               # Task 3 — public router
├── main.py                                     # Task 3 — include router
└── graphql/{types,resolvers}.py                # Task 4
apps/web/
├── public/widget.js                            # Task 5 — loader
├── src/lib/sse.ts                              # Task 5 — parseSSEFrames split
├── src/lib/widget-api.ts (+test)               # Task 5 — session, conversation, streamWidgetChat, toWidgetEvent
├── src/lib/frame-policy.ts (+test)             # Task 5 — frameAncestorsHeader, cache
├── src/lib/security-headers.ts (+test)         # Task 5 — exclude /embed
├── src/middleware.ts                           # Task 5 — /embed matcher
├── src/app/embed/[publicKey]/{layout,page}.tsx # Task 5
├── src/components/widget/WidgetChat.tsx (+test)# Task 5
├── src/components/agents/WidgetCard.tsx (+test)# Task 6
├── src/lib/widget-snippet.ts (+test)           # Task 6
├── src/app/dashboard/conversations/page.tsx    # Task 6
infrastructure/widget-demo/index.html           # Task 7
docs/PHASE-8.md                                 # Task 7
```

---

### Task 1: Widget settings, origin rules, and the public resolve

**Files:**
- Create: `app/db/models/widget.py`, `alembic/versions/0016_widget_settings.py`, `app/widget/__init__.py`, `app/widget/origins.py`, `app/widget/schemas.py`, `app/widget/service.py`
- Modify: `app/db/models/__init__.py`, `tests/integration/test_migrations.py` (RLS table list), the isolation test that enumerates tenant tables (`tests/integration/test_isolation_layers.py` or `test_rls.py` — whichever lists them)
- Test: `tests/unit/test_widget_origins.py`, `tests/integration/test_widget_service.py`

**Interfaces produced:**

```python
# app/db/models/widget.py
class WidgetPosition(StrEnum): RIGHT = "right"; LEFT = "left"
class WidgetSettings(Base, TenantMixin, TimestampMixin, UUIDMixin):   # same mixins as ApiKey
    __tablename__ = "widget_settings"
    agent_id: Mapped[uuid.UUID]          # FK agents.id ON DELETE CASCADE, unique
    enabled: Mapped[bool]                # default False
    allowed_origins: Mapped[list[str]]   # ARRAY(Text), server_default '{}'
    brand_color: Mapped[str]             # String(7), default "#2563eb"
    position: Mapped[WidgetPosition]     # enum widget_position, default RIGHT
    title: Mapped[str | None]            # String(60)
    daily_message_cap: Mapped[int]       # default 500, CHECK 1..100000

# app/widget/origins.py  (no I/O)
MAX_ALLOWED_ORIGINS = 20
class InvalidOriginError(ValueError): ...           # message names the offending value
def normalize_origin(value: str, *, allow_localhost_http: bool) -> str
def normalize_origins(values: Sequence[str], *, allow_localhost_http: bool) -> list[str]  # strip blanks, dedupe preserving order, cap

# app/widget/schemas.py
class UpdateWidgetSettingsInput(BaseModel):
    enabled: bool
    allowed_origins: list[str]
    brand_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")   # stored lower-case
    position: WidgetPosition
    title: str | None = Field(default=None, max_length=60)   # stripped; "" -> None
    daily_message_cap: int = Field(ge=1, le=100_000)

# app/widget/service.py
@dataclass(frozen=True, slots=True)
class WidgetView:                    # defaults when no row exists
    agent_id: uuid.UUID; enabled: bool; allowed_origins: list[str]; brand_color: str
    position: WidgetPosition; title: str | None; daily_message_cap: int

@dataclass(frozen=True, slots=True)
class PublicWidget:                  # an AVAILABLE widget, resolved from a public key
    organization_id: uuid.UUID; agent_id: uuid.UUID; agent_name: str
    settings: WidgetView; greeting: str | None; fallback_message: str

class WidgetSettingsService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None
    async def get(self, agent_id) -> WidgetView                        # any member; NotFoundError cross-tenant agent
    async def update(self, agent_id, data: UpdateWidgetSettingsInput) -> WidgetView   # owner/admin; upsert

async def resolve_public_key(public_key: str) -> tuple[uuid.UUID, uuid.UUID] | None
    # untenanted_session(): SELECT * FROM resolve_widget(:key); None when absent.
    # Returns None without a DB call unless public_key matches r"^pk_[A-Za-z0-9_-]{16,60}$".
async def load_available(session: AsyncSession, organization_id, agent_id) -> PublicWidget | None
    # inside the caller's tenant_session: agent (explicit org predicate) + config + settings row;
    # None unless agent.status is ACTIVE and settings.enabled
```

**Requirements:**
- Migration per spec §3: table, `UNIQUE (agent_id)`, `CHECK (daily_message_cap BETWEEN 1 AND 100000)`, enum type `widget_position`, `enable_rls(op, "widget_settings")`, and:
  ```sql
  CREATE FUNCTION resolve_widget(p_public_key text)
  RETURNS TABLE (organization_id uuid, agent_id uuid)
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp
  AS $$ SELECT organization_id, id FROM agents WHERE public_key = p_public_key $$;
  REVOKE ALL ON FUNCTION resolve_widget(text) FROM PUBLIC;
  GRANT EXECUTE ON FUNCTION resolve_widget(text) TO app_user;
  ```
  Downgrade drops the function, the table and the enum type; round-trips.
- `normalize_origin`: `urllib.parse.urlsplit`; scheme must be `https`, or `http` only when `allow_localhost_http` and host is `localhost`/`127.0.0.1`; host required, lower-cased, IDNA-encoded (`host.encode("idna").decode()`), no `*`; no userinfo, path other than `""`/`"/"`, query or fragment; default port (443 https / 80 http) dropped; result `f"{scheme}://{host}[:{port}]"`. Any violation → `InvalidOriginError(f"{value!r} is not a valid origin: <reason>")`.
- `update`: role owner/admin else `PermissionDeniedError`; agent ownership SELECT with explicit org predicate (`NotFoundError` otherwise); origins through `normalize_origins(..., allow_localhost_http=settings.environment == "local")`; `InvalidOriginError` → `ValidationError` carrying its message; upsert via `INSERT ... ON CONFLICT (agent_id) DO UPDATE` (`sqlalchemy.dialects.postgresql.insert`).

**Tests:**
- `test_widget_origins.py` (table-driven): `https://Shop.Example.com/` → `https://shop.example.com`; `https://a.com:443` → `https://a.com`; `https://a.com:8443` kept; IDN host encoded; rejected: `http://a.com`, `https://a.com/path`, `https://*.a.com`, `https://u:p@a.com`, `https://a.com?x=1`, `https://a.com#f`, `ftp://a.com`, `a.com`, `""`; `http://localhost:3000` accepted only with the flag; dedupe preserves first order; 21 origins → error.
- `test_widget_service.py`: `get` with no row returns defaults (`enabled=False`, `[]`, `#2563eb`, right, None, 500); `update` inserts then updates (one row); member → `PermissionDeniedError`; cross-tenant agent → `NotFoundError` for both methods; invalid origin → `ValidationError` naming the value; `resolve_public_key` works as `app_user` with **no** tenant set, returns `None` for an unknown or malformed key (malformed: assert no query via a spy or by passing `"x"`); `load_available` returns `None` for draft, disabled, not-enabled, and a `PublicWidget` for active+enabled with the config's greeting and fallback; RLS: tenant B cannot select tenant A's row.
- Migration: `widget_settings` in the RLS list; `0016` upgrade/downgrade round-trip.

- [ ] Record baselines (`cd apps/api && uv run pytest -q | tail -1`, `cd apps/web && npx vitest run | tail -3`).
- [ ] Write the unit and integration tests; run them; confirm they fail for the missing modules.
- [ ] Implement model, migration, origins, schemas, service; `uv run alembic upgrade head`.
- [ ] Run the new tests, then the full backend suite; `ruff check .`, `ruff format --check .`, `mypy --strict app/`.
- [ ] Commit `feat(widget): widget settings, origin rules and public-key resolve (Phase 8 Task 1)`.

---

### Task 2: Chat plumbing for anonymous visitors

**Files:**
- Create: `app/api/streaming.py`, `app/widget/events.py`
- Modify: `app/api/chat.py` (import from `streaming.py`; no behaviour change), `app/chat/service.py`, `app/leads/service.py`
- Test: `tests/unit/test_widget_events.py`, extend `tests/integration/test_chat_service.py` and `tests/integration/test_create_lead_tool.py`; `tests/integration/test_chat_endpoint.py` must pass with import changes only

**Interfaces consumed:** none from Task 1.

**Interfaces produced:**

```python
# app/api/streaming.py  — moved verbatim from api/chat.py, plus one parameter
PayloadFn = Callable[[ChatEvent], dict[str, object] | None]   # None = don't send this event
def event_payload(event: ChatEvent) -> dict[str, object]       # was _event_payload
async def stream_body(
    events: AsyncIterator[ChatEvent],
    first_event: ChatEvent,
    session_cm: AbstractAsyncContextManager[AsyncSession],
    title_conversation_id: uuid.UUID | None,
    organization_id: uuid.UUID,
    *,
    payload_fn: PayloadFn = event_payload,
) -> AsyncIterator[bytes]
SSE_HEADERS: dict[str, str]    # the three headers chat_stream sets today
# The internal-error branch builds its error through payload_fn too, so the widget's
# projection applies to it.

# app/widget/events.py  (no I/O)
PUBLIC_ERROR_MESSAGE = "Something went wrong. Please try again."
def project_public_event(event: ChatEvent) -> dict[str, object] | None

# app/chat/service.py
async def send(..., visitor_id: str | None = None) -> AsyncIterator[ChatEvent]
```

`project_public_event`, exactly per spec §4.1:

```python
def project_public_event(event: ChatEvent) -> dict[str, object] | None:
    if isinstance(event, ChatMessageStart):
        return {"type": "message_start", "conversation_id": str(event.conversation_id),
                "message_id": str(event.message_id)}
    if isinstance(event, ChatTextDelta):
        return {"type": "text_delta", "text": event.text}
    if isinstance(event, ChatToolCallStart):
        return {"type": "tool_call_start", "calls": [{"name": c.name} for c in event.calls]}
    if isinstance(event, ChatToolCallEnd):
        return None
    if isinstance(event, ChatCitations):
        seen: set[tuple[str, int | None]] = set()
        out: list[dict[str, object]] = []
        for c in event.citations:
            if c.chunk_id is None:          # product-only citation
                continue
            key = (c.document_title, c.page)
            if key not in seen:
                seen.add(key)
                out.append({"document_title": c.document_title, "page": c.page})
        return {"type": "citations", "citations": out} if out else None
    if isinstance(event, ChatMessageEnd):
        return {"type": "message_end"}
    if isinstance(event, ChatError):
        return {"type": "error", "code": event.code, "message": PUBLIC_ERROR_MESSAGE}
    raise AssertionError(f"unhandled ChatEvent variant: {event!r}")  # pragma: no cover
```

**Requirements:**
- `streaming.py` is a move, not a rewrite: comments and docstrings travel with the code. `chat.py` keeps `ChatStreamRequest`, `get_chat_provider`, the rate limit and the route. `stream_body` skips a `None` payload (no frame) but still tracks `ChatMessageEnd` for the discarded-usage log.
- `send(visitor_id=...)`: on create, pass `CreateConversationInput(channel=channel, visitor_id=visitor_id)`; on an existing conversation, when `visitor_id is not None and conversation.visitor_id != visitor_id`, raise `NotFoundError("conversation not found")` — in the same pre-`yield` span as the agent-mismatch check, with a comment pointing at it.
- `LeadService.create`: select `Conversation.channel` in the existing tenancy query and set `Lead.source = channel.value`.

**Tests:**
- `test_widget_events.py`: one test per event type; citations dedupe; product-only dropped; all-product citations → `None`; error message replaced; assert the serialized JSON of a projected tool start/end/message_end has none of the keys `arguments`, `result`, `cost_usd`, `model`, `usage`, `excerpt`.
- `test_chat_service.py`: a new conversation stores `visitor_id` and `channel=widget`; continuing with the same `visitor_id` works; a different `visitor_id` → `NotFoundError` and no message appended (count messages); `visitor_id=None` on an existing visitor conversation still works (the dashboard path).
- `test_create_lead_tool.py`: a lead from a widget conversation has `source == "widget"`; from a playground conversation, `"playground"`.
- Full `test_chat_endpoint.py` green.

- [ ] Tests first; run; confirm failures.
- [ ] Move the streaming code; implement projection, `visitor_id`, lead source.
- [ ] Full backend suite; gates; commit `feat(chat): visitor-scoped conversations and a public event projection (Phase 8 Task 2)`.

---

### Task 3: Visitor tokens and the public widget API

**Files:**
- Create: `app/widget/tokens.py`, `app/api/widget.py`
- Modify: `app/main.py` (include router), `app/core/config.py` (`widget_token_days: int = 30`), `.env.example`
- Test: `tests/unit/test_widget_tokens.py`, `tests/integration/test_widget_api.py`

**Interfaces consumed:** Task 1 `resolve_public_key`, `load_available`, `PublicWidget`; Task 2 `stream_body`, `SSE_HEADERS`, `project_public_event`, `send(visitor_id=...)`.

**Interfaces produced:**

```python
# app/widget/tokens.py
WIDGET_TOKEN_TYPE = "widget"
@dataclass(frozen=True, slots=True)
class VisitorClaims:
    organization_id: uuid.UUID; agent_id: uuid.UUID; visitor_id: str; expires_at: datetime
def create_visitor_token(*, organization_id, agent_id, visitor_id: str) -> tuple[str, datetime]
def decode_visitor_token(token: str) -> VisitorClaims     # AuthenticationError on bad signature/expiry/typ
```

Build on `app/core/security.py`'s `_encode` and the same `jwt.decode` call (secret, algorithm). If `decode_token`'s `TokenPayload` cannot carry `agent`/`vid`, add a sibling decoder in `security.py` rather than loosening `TokenPayload`; `decode_token(expected_type="access")` must keep refusing a widget token (test it).

**Endpoints** (router prefix `/api/v1/widget`, spec §4 table):

```python
class WidgetConfigOut(BaseModel):
    agent_name: str; title: str | None; greeting: str | None
    fallback_message: str        # public copy the owner wrote for visitors; shown on a failed turn
    brand_color: str; position: WidgetPosition
class WidgetSessionOut(BaseModel):
    token: str; expires_at: datetime; config: WidgetConfigOut
class WidgetMessageOut(BaseModel):
    role: Literal["user", "assistant"]; text: str
class WidgetConversationOut(BaseModel):
    conversation_id: uuid.UUID; messages: list[WidgetMessageOut]
class WidgetChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: uuid.UUID | None = None

GET  /{public_key}/frame-policy -> {"allowed_origins": list[str]}      # always 200
POST /{public_key}/session      -> WidgetSessionOut                     # optional Bearer
GET  /conversation              -> WidgetConversationOut | None         # Bearer required
POST /chat/stream               -> text/event-stream                    # Bearer required
```

**Requirements:**
- One dependency, `visitor_from_bearer(request) -> VisitorClaims`: reads `Authorization: Bearer`, `decode_visitor_token`, else 401 `AuthenticationError`. Never imports `app.auth`.
- One helper, `_available_or_404(org_id, agent_id, session) -> PublicWidget` wrapping `load_available`; every bearer route calls it inside its `tenant_session(TenantContext(org, None, None, request_id_var.get()))` so a disable takes effect on the next call.
- `session`: rate-limit `widget:session:ip:{ip}` (20/3600) **only when minting a new `vid`**; if the optional bearer decodes and its `agent_id` equals the resolved agent, reuse its `vid`; otherwise `str(uuid.uuid4())`. Unknown key or unavailable → `NotFoundError("widget not found")`. Log rejections with `public_key[:8]` only.
- `conversation`: most recent `open` conversation where `agent_id = claims.agent_id AND visitor_id = claims.visitor_id AND channel = widget` (explicit org predicate), last 50 user/assistant messages oldest-first via `ConversationService.history`; `null` when none.
- `chat/stream`: limits in this order — `widget:msg:visitor:{vid}` (10/60), `widget:msg:ip:{ip}` (30/60), `widget:msg:agent-day:{agent}:{yyyymmdd UTC}` (cap/86400). The daily cap raises a `RateLimitError` subclass `WidgetDailyCapError` with code `widget_daily_cap` (add the code the same way other `AppError` codes are defined in `app/core/errors.py`). Then the same manual-session / first-event pre-read / `StreamingResponse` shape as `chat_stream`, calling `ChatService(...).send(claims.agent_id, message, conversation_id=..., channel=ConversationChannel.WIDGET, visitor_id=claims.visitor_id)` and `stream_body(..., title_conversation_id=None, payload_fn=project_public_event)` (widget conversations are not titled — `TITLED_CHANNELS` is unchanged). `get_chat_provider` is reused as the provider dependency so tests inject `FakeProvider`.
- `frame-policy`: limit `widget:frame:ip:{ip}` (120/60); returns the settings' origins only when available, else `[]`.
- IP via `app.api.auth._client_key` (move it to `app/core/request.py` as `client_ip(request)` if importing a private name across routers bothers ruff; keep one implementation).

**Tests** (`test_widget_api.py`, `FakeProvider` for chat; seed an active agent with `WidgetSettingsService.update(enabled=True, allowed_origins=["https://shop.example.com"])`):
- session: mints token + config; replaying with that token keeps `vid` (decode both); a token for another agent mints a new `vid`; unknown key, draft agent, disabled agent, `enabled=False` → identical 404 body.
- **Review Focus 2:** get a token, then disable the widget → `conversation` 404 and `chat/stream` 404; re-enable, set agent `draft` → 404.
- tokens: dashboard access token on `/api/v1/widget/conversation` → 401; widget token on `/api/v1/chat/stream` → 401 and on `/graphql` (`{ me { id } }` or the closest authenticated query) → authentication error; expired token (monkeypatch lifetime negative) → 401; garbage → 401.
- **Review Focus 1:** visitor A streams a turn; visitor B's token + A's `conversation_id` → 404; A's conversation still has exactly A's messages; B's `conversation` returns `null`; a `conversation_id` of the same visitor on another agent → 404.
- resume: after two turns, `conversation` returns 4 messages in order; a closed conversation is not returned.
- **Review Focus 3:** a `FakeProvider` script that calls `search_products` then answers; scan the raw SSE body — no `"arguments"`, `"result"`, `"excerpt"`, `"cost_usd"`, `"model"`; a `tool_call_start` frame with `{"name": "search_products"}` present.
- limits: visitor limit (monkeypatch to 2) → 429 on the 3rd; daily cap = 1 → second message 429 with code `widget_daily_cap`; new-session limit counts only minted tokens.
- message of 2001 chars → 422; empty → 422.
- `frame-policy`: origins when available, `[]` for draft/unknown, always 200.
- a lead created through the widget has `source == "widget"` and `conversations.channel == widget`.
- captured logs (structlog capture used elsewhere in the suite) contain neither the visitor id nor the full public key.
- `test_widget_tokens.py`: round-trip; `typ` enforced both ways; tampered signature → `AuthenticationError`.

- [ ] Tests first; implement; full suite; gates; commit `feat(widget): visitor tokens and the public widget API (Phase 8 Task 3)`.

---

### Task 4: GraphQL surface for widget settings

**Files:**
- Modify: `app/graphql/types.py`, `app/graphql/resolvers.py`, `packages/shared/schema.graphql` (regenerate)
- Test: `tests/integration/test_graphql_widget.py`

**Interfaces consumed:** Task 1 `WidgetSettingsService`, `WidgetView`, `UpdateWidgetSettingsInput`.

**Interfaces produced (GraphQL):**

```graphql
enum WidgetPosition { RIGHT LEFT }
type WidgetSettings {
  agentId: UUID! enabled: Boolean! allowedOrigins: [String!]! brandColor: String!
  position: WidgetPosition! title: String dailyMessageCap: Int!
}
input UpdateWidgetSettingsInput {
  enabled: Boolean! allowedOrigins: [String!]! brandColor: String!
  position: WidgetPosition! title: String dailyMessageCap: Int!
}
extend type Agent { publicKey: String! }
extend type Lead  { source: String }      # set by Task 2; null for leads created before it
Query:    widgetSettings(agentId: UUID!): WidgetSettings!
Mutation: updateWidgetSettings(agentId: UUID!, input: UpdateWidgetSettingsInput!): WidgetSettings!
```

**Requirements:** inputs through `_build`; `ValidationError` from an invalid origin renders with its existing code and the offending value in the message; `PermissionDeniedError` for members on the mutation; unauthenticated → `authentication required`. `publicKey` is safe to expose to any member (it is public by design; spec §2). `Lead.source` is a plain passthrough of the column.

**Tests:** defaults for a fresh agent; update round-trips and normalizes (`https://Shop.Example.com/` comes back `https://shop.example.com`); invalid origin → error naming it, nothing stored; member → permission error; cross-tenant agent → not found; `agent { publicKey }` starts with `pk_`; `leads { source }` returns the stored value; SDL regenerated and diff-free (`cd apps/api && uv run strawberry export-schema app.graphql.schema:schema > ../../packages/shared/schema.graphql`).

- [ ] Tests first; implement; regenerate SDL; gates; commit `feat(api): GraphQL surface for widget settings (Phase 8 Task 4)`.

---

### Task 5: Loader, embed page, and frame policy

**Files:**
- Create: `apps/web/public/widget.js`, `src/lib/widget-api.ts` (+ `.test.ts`), `src/lib/frame-policy.ts` (+ `.test.ts`), `src/app/embed/[publicKey]/layout.tsx`, `src/app/embed/[publicKey]/page.tsx`, `src/components/widget/WidgetChat.tsx` (+ `.test.tsx`), `src/lib/widget-loader.test.ts`
- Modify: `src/lib/sse.ts`, `src/lib/security-headers.ts` (+ its test), `src/middleware.ts`

**Interfaces consumed:** Task 3's endpoints and their JSON shapes; Task 2's projected event shapes.

**Interfaces produced:**

```ts
// src/lib/sse.ts
export async function* parseSSEFrames<E>(
  chunks: AsyncIterable<Uint8Array>, toEvent: (value: unknown) => E | null,
): AsyncGenerator<E, void, void>
export function parseSSEStream(chunks) { return parseSSEFrames(chunks, toSSEEvent); }  // unchanged behaviour

// src/lib/widget-api.ts
export type WidgetConfig = { agentName: string; title: string | null; greeting: string | null;
                             fallbackMessage: string; brandColor: string; position: "right" | "left" };
export type WidgetSession = { token: string; expiresAt: string; config: WidgetConfig };
export type WidgetEvent =
  | { type: "message_start"; conversation_id: string; message_id: string }
  | { type: "text_delta"; text: string }
  | { type: "tool_call_start"; calls: { name: string }[] }
  | { type: "citations"; citations: { document_title: string; page: number | null }[] }
  | { type: "message_end" }
  | { type: "error"; code: string; message: string };
export function toWidgetEvent(value: unknown): WidgetEvent | null
export class WidgetUnavailableError extends Error {}
export async function startSession(apiUrl: string, publicKey: string, token: string | null): Promise<WidgetSession>  // 404 -> WidgetUnavailableError
export async function loadConversation(apiUrl: string, token: string):
  Promise<{ conversationId: string; messages: { role: "user" | "assistant"; text: string }[] } | null>
export async function streamWidgetChat(p: { apiUrl: string; token: string; message: string;
  conversationId: string | null; onEvent: (e: WidgetEvent) => void; signal?: AbortSignal }): Promise<void>
  // non-200 -> onEvent({type:"error", code, message}) exactly like streamChat; network error -> "network_error"
export function readStoredToken(publicKey: string): string | null      // try/catch localStorage
export function storeToken(publicKey: string, token: string): void     // try/catch
export const TOOL_LABELS: Record<string, string>   // search_products -> "Searching products…", get_product -> "Looking up a product…", retrieve_knowledge -> "Checking our information…", create_lead -> "Saving your details…"; unknown -> "Working…"

// src/lib/frame-policy.ts  (no I/O except fetchFrameOrigins)
export function isBareOrigin(value: string): boolean     // ^https?://[a-z0-9.-]+(:\d{1,5})?$ after lower-casing
export function frameAncestorsHeader(origins: string[]): string   // "frame-ancestors 'self' a b"; non-bare values dropped
export async function fetchFrameOrigins(apiBase: string, publicKey: string, now?: () => number): Promise<string[]>
  // module-level Map cache, 30_000 ms TTL, max 500 entries (evict oldest); any failure -> [] (not cached)
```

**Requirements:**
- `sse.ts`: extract the frame loop from `parseSSEStream` into `parseSSEFrames`; every existing `sse` test passes unchanged.
- `security-headers.ts`: the `/:path*` rule becomes `source: "/((?!embed/).*)"` with the same headers; add a rule for `/embed/:path*` with every header **except** `Content-Security-Policy` and `X-Frame-Options`. Update its test to assert both, and that `/dashboard` still gets `frame-ancestors 'none'`.
- `middleware.ts`: matcher `["/dashboard/:path*", "/embed/:path*"]`. `/dashboard` branch unchanged. `/embed/<key>` branch: `NextResponse.next()` with `Content-Security-Policy: frame-ancestors(...)` from `fetchFrameOrigins(proxyTargetUrl(process.env.API_INTERNAL_URL, API_URL), key)`. Never redirects to `/login`.
- `embed/[publicKey]/layout.tsx`: own minimal layout — no `AuthProvider`, no urql — only `globals.css` and `<body className="bg-transparent">`. If the root layout wraps every route in providers, move those providers into `src/app/dashboard/layout.tsx` and `src/app/(auth)/layout.tsx` instead, and say so in the report.
- `WidgetChat` (client): props `{ apiUrl: string; publicKey: string }`. Flow per spec §6: `readStoredToken` → `startSession` → `storeToken` → `loadConversation` → render messages (or the greeting as a first assistant bubble) → composer (1–2000 chars, counter past 1800). Assistant text renders through the existing `AnswerText` (safe markdown); user text as plain text. Streaming appends `text_delta`; `tool_call_start` shows `TOOL_LABELS` under the streaming bubble; `citations` renders "Sources: title (p. n)" as text. On `error`, the bubble shows `config.fallbackMessage` (from the session response). `widget_daily_cap` and `WidgetUnavailableError` → "This assistant is not available right now." Header: title (or agent name), a "New conversation" button, a close button that posts `{type:"close"}`. On mount it posts `{type:"ready", brand_color, position, title}` to `window.parent` with target `"*"`; the brand colour is applied through a CSS custom property on the root element (`style={{"--widget-brand": color}}`), never interpolated into class names.
- `public/widget.js` per spec §6 steps 1–5; plain ES2017 IIFE; no dependencies; no `innerHTML` with any variable; the iframe gets `title="Chat"`, `allow=""`, and `referrerpolicy="strict-origin-when-cross-origin"`. Export nothing to `window` except an idempotence guard `window.__saWidgetLoaded`.

**Tests:**
- `frame-policy.test.ts`: header with 0/1/2 origins; a returned `https://a.com/path` or `*` is dropped (**Review Focus 4**); cache hit within 30 s, miss after, failures not cached (inject `now` and a mocked `fetch`).
- `security-headers.test.ts`: as above.
- `widget-api.test.ts`: `toWidgetEvent` accepts each projected shape and rejects the playground's full shapes missing fields it needs; `startSession` 404 → `WidgetUnavailableError`; stored-token helpers survive a throwing `localStorage`.
- `WidgetChat.test.tsx` (mock `widget-api`): shows greeting when no conversation; renders a resumed conversation; sends and streams a reply; shows a tool label then removes it on the next `text_delta`; shows the fallback on `error`; shows the unavailable message on `WidgetUnavailableError`; posts `ready` to parent; "New conversation" clears messages and sends the next turn with `conversationId: null`.
- `widget-loader.test.ts` (jsdom; load `public/widget.js` text and `new Function` it with a fake `document.currentScript`): creates one host element; second load is a no-op; clicking the launcher creates the iframe with the right `src`; a `message` event from the wrong origin or wrong source is ignored; a valid `close` hides the iframe; missing `data-key` → no element and one `console.warn`.
- `sse` tests unchanged and green.

- [ ] Tests first; implement; `npx tsc --noEmit`, `npx eslint .`, `npx vitest run`, `npx next build`; commit `feat(web): widget loader, embed page and per-agent frame policy (Phase 8 Task 5)`.

---

### Task 6: Website widget card and Conversations page

**Files:**
- Create: `src/components/agents/WidgetCard.tsx` (+ `.test.tsx`), `src/lib/widget-snippet.ts` (+ `.test.ts`), `src/app/dashboard/conversations/page.tsx` (+ a test for its pure helpers if any)
- Modify: `src/app/dashboard/agents/[id]/page.tsx`, `src/components/shell/nav.ts`, `src/app/dashboard/leads/page.tsx`, `src/graphql/operations.graphql`, `src/graphql/generated.ts` (codegen), `docs/DESIGN.md`

**Interfaces consumed:** Task 4 GraphQL; Task 5 `/embed/[publicKey]`.

**Interfaces produced:**

```ts
// src/lib/widget-snippet.ts
export function widgetSnippet(appOrigin: string, publicKey: string): string
  // `<script src="${appOrigin}/widget.js" data-key="${publicKey}" async></script>`; attribute values HTML-escaped
export function parseOriginLines(text: string): string[]    // split on newlines/commas, trim, drop blanks
```

**Requirements (spec §7):**
- **Read `docs/DESIGN.md` first; update it after** with the widget card, the preview frame and the conversations page patterns.
- `WidgetCard` props `{ agentId: string; publicKey: string; agentStatus: AgentStatus; leadToolEnabled: boolean; canEdit: boolean }`. Fields: enable toggle, allowed-domains textarea (one per line), brand colour (`<input type="color">` + hex text), position select, title, daily cap. Save runs `updateWidgetSettings`; server errors render inline via `firstGraphQLError`. Snippet with copy button (`widgetSnippet(window.location.origin, publicKey)`). Live preview: an `<iframe src="/embed/{publicKey}">` in a 360×560 frame, `key` bumped after each successful save to reload it; when `enabled` is false, show a placeholder explaining the preview appears once enabled. Warnings per spec §7 (agent not active; enabled with no domains; `create_lead` granted; domain list ≠ API protection — the last one always visible as help text). `canEdit=false` → read-only fields, no save.
- Agent page: render `WidgetCard` below `McpAccessCard`; `canEdit` from the same role source `McpAccessCard` uses; `leadToolEnabled` from the Tools card's data (refetch when tools toggle, the way `McpAccessCard` refreshes its exposed tools).
- Conversations page: agent `Select` (first agent by default; `?agent=`), channel `Select` (Widget default, Playground, API, All), list via `conversations(agentId, channel, limit: 50, offset)` with "Load more", row shows `title ?? preview ?? "Untitled"` and `last_message_at` relative time; selecting a row (or `?conversation=<id>`) loads `conversation(id)` messages and renders them read-only with `ChatMessage` via `conversation-transcript.ts`. Empty state for no widget conversations links to the agent's widget card.
- `nav.ts`: add "Conversations" between Playground and Leads (match existing icon style).
- Leads page: each lead row links to `/dashboard/conversations?agent=<agentId>&conversation=<conversationId>`; show the lead's `source` (Task 4) as a badge when it is not null.

**Tests:** `widget-snippet`: escaping, origin trailing slash trimmed; `parseOriginLines`. `WidgetCard`: loads settings into fields; save sends normalized input and shows a server origin error inline; warnings appear for draft agent / no domains / lead tool; read-only for `canEdit=false`; copy button copies the snippet; preview iframe points at `/embed/<key>` and is absent when disabled. Conversations page: renders list for the default agent, opens `?conversation=` on load.

- [ ] Read `docs/DESIGN.md`; tests first; implement; codegen (`cd apps/web && npm run codegen`); gates; update `docs/DESIGN.md`; commit `feat(web): website widget card and conversations page (Phase 8 Task 6)`.

---

### Task 7: Demo page, docs, and end-to-end verification

**Files:**
- Create: `infrastructure/widget-demo/index.html`, `docs/PHASE-8.md`
- Modify: `Makefile` (`widget-demo` target), `app/db/seed.py` (enable the demo agent's widget with `http://localhost:5500` allowed), `README.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `.env.example`

**Requirements:**
- `index.html`: a plain fake storefront page with the snippet pointing at `http://localhost:3000/widget.js` and a `data-key` placeholder the README tells you to replace (or read from `?key=` and inject the script tag — prefer this, so no editing is needed). `make widget-demo` → `cd infrastructure/widget-demo && python -m http.server 5500`.
- Seed: the demo agent gets `enabled=True`, `allowed_origins=["http://localhost:5500"]` (seed runs only in `local`, so the localhost-http rule applies); print the demo URL with its key.
- `PHASE-8.md` in the shape of `PHASE-7.md`: what shipped, the data model, identity, endpoints, projection, limits, frame policy, dashboard, and §9 "Not delivered" copied from spec §9 plus anything found during implementation; state plainly that the domain list does not protect the API (spec §2, §10).
- README: roadmap row 8 → "In progress — widget delivered (see PHASE-8.md); billing, plans, analytics not started"; how to try the widget locally; the new env var. ARCHITECTURE.md: `widget_settings` in the schema section, the widget in the request-flow section, `public_key` note updated. DEPLOYMENT.md: the embed route must not be framed-blocked by a proxy adding `X-Frame-Options`; `/api/v1/widget/*` needs the same SSE proxy settings as chat; rate limits key on the proxy IP unless forwarded headers are handled (same caveat as login).

**Verification:**
- [ ] `git fetch origin && git merge origin/main`.
- [ ] Full backend and web suites green (report baseline → final counts); all gates; single alembic head; `0016` round-trip; SDL + codegen fresh.
- [ ] Live: `docker compose up -d --build --wait`; `cd apps/api && uv run python -m app.db.seed`; `make`-equivalent `python -m http.server 5500` in `infrastructure/widget-demo`; open `http://localhost:5500/?key=<pk>` in a browser (use the `run` skill / a headless browser if available): launcher appears, chat streams, a product question shows the tool label, reload restores the conversation, the conversation appears on `/dashboard/conversations`. Then remove `localhost:5500` from allowed domains in the card and confirm the iframe is refused (console CSP error) while the dashboard preview still loads.
- [ ] Whole-branch review, one fix wave, one scoped re-review, adjudicate residuals.
- [ ] Commit docs `docs: record Phase 8 widget as delivered`; push; open a PR.

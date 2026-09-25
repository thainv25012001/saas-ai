# Embeddable Chat Widget — Design

**Goal:** A business pastes one `<script>` tag into its website. A site visitor, without an
account, opens a chat bubble and talks to that business's agent — the same agent, tools,
knowledge and products the owner tested in the playground.

**Scope:** The first slice of Phase 8 (SaaS features). A loader script, a public embed page,
visitor identity with resume-after-reload, a public chat stream, per-agent widget settings
(enable, allowed domains, appearance, daily cap), lead capture from the widget, and a
dashboard page for reading widget conversations. No billing, plans, analytics or retention.

**Approach in one line:** A tiny loader draws a launcher and an iframe onto our own
`/embed/[publicKey]` page; the browser enforces the domain allow-list through
`frame-ancestors`, and the API treats every widget request as untrusted and rate-limited.

---

## 1. Current state

Most of the data model anticipated the widget; nothing uses it.

- **`Agent.public_key`** (`pk_…`, globally unique, `String(64)`) is generated at agent
  creation ([`agents/service.py:82`](../../../apps/api/app/agents/service.py#L82)) and
  described in `docs/ARCHITECTURE.md` as the widget embed key. Nothing reads it. There is no
  lookup by it that works before the tenant is known.
- **`ConversationChannel.WIDGET`** and **`conversations.visitor_id`** (`String(255)`,
  nullable) exist. `ChatService.send` never passes `visitor_id`, and
  the chat route passes `channel=PLAYGROUND`.
- **`AgentConfig.greeting`** and **`AgentConfig.fallback_message`** exist and are the
  widget's opening line and its error text.
- **Chat** is `POST /api/v1/chat/stream`
  ([`api/chat.py:413`](../../../apps/api/app/api/chat.py#L413)): JWT bearer only, 30/min
  keyed by user (falling back to organization — the comment there names the widget), SSE
  events `message_start`, `citations`, `text_delta`, `tool_call_start`, `tool_call_end`,
  `message_end`, `error`. It does **not** check agent status.
- **Pre-tenant resolution precedent:** Phase 7's `resolve_api_key(p_hash)` SECURITY DEFINER
  function returns only ids; everything else is read inside a normal `tenant_session`
  ([`mcp/auth.py:84`](../../../apps/api/app/mcp/auth.py#L84)).
- **Tokens** are HS256 JWTs whose `typ` claim is checked by
  `decode_token(expected_type=…)` ([`core/security.py:77`](../../../apps/api/app/core/security.py#L77)).
- **Framing is forbidden everywhere.** The API
  ([`core/security_headers.py`](../../../apps/api/app/core/security_headers.py)) and the web
  app ([`lib/security-headers.ts`](../../../apps/web/src/lib/security-headers.ts)) both send
  `frame-ancestors 'none'` and `X-Frame-Options: DENY` on every route.
- **CORS** is one global allow-list (`CORS_ORIGINS`, credentials on).
- **Web auth middleware** matches only `/dashboard/:path*`, so a route outside it is public.
- **Reading conversations:** GraphQL `conversations(agentId, channel, limit, offset)` and
  `conversation(id)` with messages already exist (built for playground history).
- **Leads** have a nullable `source String(100)` that nothing sets.

## 2. Approaches considered

**A — Loader + iframe (chosen).** `widget.js` draws the launcher and an iframe pointing at
`/embed/[publicKey]` on our web app. The iframe calls the API from our own origin, so CORS
does not change. The domain restriction is `frame-ancestors`, enforced by the browser. The
chat reuses the existing React components and the markdown renderer. Host-page CSS and
script cannot reach into the widget and vice versa. The visitor token lives in the iframe's
`localStorage`, which modern browsers partition per top-level site — one visitor identity
per business site, which is the right meaning — and which works with third-party cookies
blocked, since no cookie is used.

**B — Shadow-DOM bundle calling the API cross-origin.** No iframe, but a second chat UI in a
standalone bundle, per-agent dynamic CORS on public routes, and an `Origin` check in the API
as the domain restriction. More code and weaker isolation for no user-visible gain.

**What neither approach does:** a domain allow-list stops *browsers* on other sites. It does
not stop `curl` with a `pk_` key — the key is public by design, it is in the page source.
The real abuse controls are §5's rate limits, the daily cap, and requiring the agent to be
`active` and the widget `enabled`. The dashboard and docs say this plainly.

## 3. Data model

Migration `0016_widget_settings`.

**`widget_settings`** — one row per agent, RLS-scoped like every tenant table.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | mixin |
| `organization_id` | uuid FK, not null | tenant mixin, RLS |
| `agent_id` | uuid FK → agents, `ON DELETE CASCADE`, **unique** | |
| `enabled` | bool, not null, default `false` | |
| `allowed_origins` | `text[]`, not null, default `{}` | normalized origins, ≤ 20 |
| `brand_color` | `varchar(7)`, not null, default `#2563eb` | `#rrggbb` |
| `position` | enum `widget_position` (`right`, `left`), default `right` | |
| `title` | `varchar(60)`, nullable | falls back to the agent name |
| `daily_message_cap` | int, not null, default `500`, check `1..100000` | visitor messages per UTC day |
| `created_at`, `updated_at` | timestamptz | mixin |

A missing row means "defaults, disabled". The row is created on first
`updateWidgetSettings`, not at agent creation, so existing agents need no backfill.

**Origin normalization** (`app/widget/origins.py`, pure function, unit-tested): accepts
`https://host[:port]` and, only when `ENVIRONMENT=local`, `http://localhost[:port]` /
`http://127.0.0.1[:port]`. Lower-cases the host, strips a trailing slash, drops a default
port, rejects any path, query, fragment, userinfo, wildcard or non-http(s) scheme. Stored
values are therefore exactly what goes into `frame-ancestors`. Duplicates are removed.

**`resolve_widget(p_public_key text)`** — SECURITY DEFINER, `search_path = public, pg_temp`,
`REVOKE ALL FROM PUBLIC`, `GRANT EXECUTE TO app_user`; returns `(organization_id, agent_id)`
for the agent with that key. Nothing else — status, settings and config are read afterwards
in a `tenant_session`, under RLS, exactly as `resolve_api_key` does.

**`ChatService.send`** gains `visitor_id: str | None = None`, passed to
`CreateConversationInput` on a new conversation. On an existing conversation, when
`visitor_id` is given and differs from `conversation.visitor_id`, it raises `NotFoundError`
— the same answer as a cross-tenant id.

**Lead source.** `LeadService.create` already loads the conversation to check tenancy; it
now reads the conversation's `channel` in that same query and sets `source` to its value
(`widget`, `playground`, `api`). No schema change, no `ToolContext` change.

## 4. Visitor identity and endpoints

A new router, `app/api/widget.py`, prefix `/api/v1/widget`. It depends on nothing in
`app/auth` — widget requests never carry a dashboard JWT.

**Resolving a widget** (`app/widget/service.py`, `WidgetService.resolve_public(public_key)`):
`resolve_widget` → `TenantContext(org, user_id=None, role=None)` → in a `tenant_session`,
load agent, config and settings. It is *available* only when the agent is `active` and
`settings.enabled` is true. Unknown key, draft, disabled and not-enabled all raise the same
`NotFoundError("widget not found")` — the public side cannot tell them apart.

**Visitor token.** HS256 with the existing `JWT_SECRET`, claims
`{typ: "widget", org, agent, vid, iat, exp}`, lifetime 30 days (`WIDGET_TOKEN_DAYS`).
`vid` is a random UUID4 string. Because `typ` is checked on decode, a widget token is
refused wherever an access token is expected and vice versa.

| Endpoint | Auth | Does |
|---|---|---|
| `GET /api/v1/widget/{public_key}/frame-policy` | none (optional `X-Widget-Frame-Secret`) | Returns `{allowed_origins: [...]}` when available, else `{allowed_origins: []}`. Always 200, so it reveals nothing more than the embed page itself would. Used by the web middleware (§6). A malformed key is answered `[]` before anything else (no Redis, no DB). Rate-limited per public key (120/min) — the intended caller is our web server, so a per-IP limit would be one budget for every agent — **except** when `WIDGET_FRAME_POLICY_SECRET` is configured and the header matches it (`hmac.compare_digest`): our own middleware is never limited, so nobody can spend a public key's budget and unframe its widget. *(Final-review ruling; supersedes the plain per-key limit.)* |
| `GET /api/v1/widget/{public_key}/config` | none | The loader's pre-draw check: `{available, brand_color, position, title}`, all nulls when unavailable. Always 200. Malformed key answered before any limiter; otherwise rate-limited per key (120/min). `Cache-Control: public, max-age=60`, `Access-Control-Allow-Origin: *` (never credentials). Reached from host pages through the web origin, which rewrites the same path to the API (§6). *(Final-review ruling.)* |
| `POST /api/v1/widget/{public_key}/session` | optional widget bearer | If the bearer is a valid widget token for this agent, keep its `vid`; otherwise mint a new one. Returns `{token, expires_at, config}` where `config = {agent_name, title, greeting, fallback_message, brand_color, position}`. |
| `GET /api/v1/widget/conversation` | widget bearer | The visitor's most recent `open` widget conversation for the token's agent, with its last 50 user/assistant messages (text only), or `null`. |
| `POST /api/v1/widget/chat/stream` | widget bearer | Body `{message (1–2000 chars), conversation_id?}`. Re-checks availability, enforces §5, then runs `ChatService.send(channel=WIDGET, visitor_id=vid)` through the same streaming body as the dashboard route, with §4.1's projection. |

Every bearer-authenticated widget request re-resolves availability, so disabling the widget
or the agent takes effect on the next message, not when tokens expire.

The streaming body (`_stream_body`, first-event pre-read, heartbeat, title enqueue) is
lifted out of `api/chat.py` into a function both routes call; the dashboard route's
behaviour does not change.

### 4.1 Public event projection

Widget visitors see less than the playground, because tool arguments and results can carry
internal data (lead ids, raw product rows, other chunks) and costs are the business's own.

| Event | Widget receives |
|---|---|
| `message_start` | unchanged |
| `text_delta` | unchanged |
| `tool_call_start` | `{calls: [{name}]}` — names only, for a "Searching products…" indicator |
| `tool_call_end` | dropped |
| `citations` | `{citations: [{document_title, page}]}` — deduplicated; product-only citations dropped |
| `message_end` | `{}` |
| `error` | `{code}` plus a fixed public message; the UI shows the agent's `fallback_message` |

The projection is one pure function (`project_public_event`) with unit tests per event.

## 5. Abuse controls

All use `enforce_rate_limit` (Redis fixed window, fails open and logs, as today).

| Key | Limit | Applies to |
|---|---|---|
| `widget:session:ip:{ip}` | 20 / hour | new-token sessions (refreshing an existing token is not counted) |
| `widget:msg:visitor:{vid}` | 10 / minute | chat stream |
| `widget:msg:ip:{ip}` | 30 / minute | chat stream |
| `widget:msg:agent-day:{agent_id}:{yyyymmdd}` | `daily_message_cap` / 86 400 s | chat stream |
| `widget:frame:key:{public_key}` | 120 / minute | frame-policy (per key, not per IP: the intended caller is our own web server, so a per-IP budget would be shared by every agent and a flood of made-up keys could unframe them all). Skipped for a request carrying the configured `WIDGET_FRAME_POLICY_SECRET`, since the key is public and anyone could otherwise keep it spent. |
| `widget:config:key:{public_key}` | 120 / minute | the loader's config check (responses are cacheable for 60 s, so real pages rarely reach the API) |

A malformed public key never reaches a limiter: frame-policy and config answer it
(`[]` / all nulls) before touching Redis or the database.

The IP is `app/core/request.py::client_ip` (`request.client.host`), exactly as the auth
routes use it. Behind a proxy that would be the proxy's address, so the API image runs
uvicorn with `--proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"`:
loopback by default, and `'*'` on Render (`render.yaml`), whose proxy is the only way in
(`docs/DEPLOYMENT.md`). *(Final-review ruling; supersedes "no forwarded-header trust".)* Over the daily cap the stream answers
`429 {code: "widget_daily_cap"}` and the widget shows "This assistant is not available
right now."

`create_lead` keeps its per-conversation limit (3 / 5 min) and remains off unless the agent
grants it. Prompt injection from anonymous visitors is the same threat the playground
already has, with one more incentive; the only write tool reachable is `create_lead`, and
it writes a row the business reviews.

**Privacy.** No IP is stored anywhere; it exists only inside rate-limit keys that expire.
`visitor_id` is a random id with no link to a person until they volunteer details to
`create_lead`. Logs never carry `visitor_id` (as `ToolContext.log_fields` already omits it)
or message text. Widget session rejections log only the key's first 8 characters.

## 6. Web: loader and embed page

**Loader — `apps/web/public/widget.js`.** Hand-written ES2017, no build step, < 5 KB. It:

1. Finds its own `<script>` (`document.currentScript`), reads `data-key`, and derives the app
   origin from the script's `src`.
2. Fetches `APP_ORIGIN/api/v1/widget/{key}/config` (no credentials; the web app rewrites that
   path to the API, `src/lib/auth-proxy.ts`'s `widgetConfigRewrites`). When the answer is
   unavailable, or the fetch fails in any way, it draws **nothing** and logs one
   `console.info`. Otherwise it creates a host element with a closed Shadow DOM holding the
   launcher, drawn from the start in the owner's `brand_color` (icon black or white by WCAG
   contrast, the same formula as `src/lib/contrast.ts`), on the owner's side, labelled with the
   title. A later `ready` message from the frame can still update all three. *(Final-review
   ruling; supersedes the neutral default launcher that ignored the off switch.)*
3. On first open, creates the iframe `APP_ORIGIN/embed/{key}` inside the shadow root
   (400 × 640 px, full-screen under 480 px wide), and toggles it afterwards. While open, the
   launcher is an "×" that closes the panel; under 480 px it moves to the top corner above the
   full-screen frame (higher z-index), so a frame that never loads, hangs or is CSP-blocked can
   still be closed.
4. Listens for `message` events **only** when `event.origin === APP_ORIGIN` and
   `event.source === iframe.contentWindow`. Messages: `ready {brand_color, position, title}`,
   `close` (an `unread {count}` message was planned and not delivered, `docs/PHASE-8.md` §7). It posts nothing but `{type: "open"}` to the iframe, targeted
   at `APP_ORIGIN`.
5. Does nothing (and logs one console warning) if `data-key` is missing.

**Embed page — `apps/web/src/app/embed/[publicKey]/page.tsx`.** A client page with its own
minimal layout (no `AuthProvider`, no urql). It:

- calls `session` (sending any stored token), stores the returned token in
  `localStorage["widget:{publicKey}"]` inside try/catch — when storage is unavailable the
  widget still works, it just does not resume;
- calls `conversation` and renders it, or the greeting;
- streams with a new `streamWidgetChat` in `src/lib/widget-api.ts`, built on the existing
  `parseSSEStream` and the widget event types;
- renders messages with the existing `ChatMessage` / `AnswerText` (the restricted markdown
  renderer), a tool indicator from `tool_call_start` names, and source titles under an
  answer;
- offers "New conversation" (drops the conversation id; the visitor id stays);
- shows "This assistant is not available right now." on a 404 from `session`, and when
  `session` or `conversation` does not answer within 10 s (`AbortSignal.timeout`);
- shows its header, with the close button, while loading and when unavailable, not only
  once ready;
- on a stream `error`, appends the fallback after any partial answer rather than replacing
  it; a `not_found` on a turn that sent a `conversation_id` (the resumed conversation is
  gone) retries that message once as a new conversation; anything thrown becomes the
  fallback, never an unhandled rejection.

It posts `ready` to `window.parent` with target origin `*` (it cannot know the host origin
and the payload is public config), and `close` likewise.

**Frame policy — `src/middleware.ts`.** The matcher gains `/embed/:path*`. For those paths
the middleware fetches `GET /api/v1/widget/{key}/frame-policy` (server-side, via the API base URL `auth-proxy.ts` already resolves from `API_INTERNAL_URL`, sending `X-Widget-Frame-Secret` from the server-only `WIDGET_FRAME_POLICY_SECRET` when it is set),
caching each answer in a module-level map for 30 s (middleware runs on the edge runtime,
where Next's fetch cache does not apply), and sets:

```
Content-Security-Policy: frame-ancestors 'self' <origins…>
```

and removes `X-Frame-Options`. A failed fetch (network, timeout, any non-200 including 429)
serves the key's last successful answer even past its 30 s TTL — the API's only caller is this
server, so one blip must not unframe live widgets. With no origins (or a failure and no earlier
success for that key) it sets `frame-ancestors 'self'` only — the dashboard preview still works, every other site is
refused. `lib/security-headers.ts` excludes `/embed/*` from the global framing headers and
keeps them for every other route. The header builder is a pure function with tests.

**Event parsing.** `parseSSEStream` validates the playground's full event shapes and
silently skips anything else, so projected widget events would be dropped. Its frame
reader is split out as `parseSSEFrames(chunks, toEvent)`; `parseSSEStream` becomes
`parseSSEFrames(chunks, toSSEEvent)` with no behaviour change, and the widget passes its
own `toWidgetEvent`.

## 7. Dashboard

**Website widget card** on `/dashboard/agents/[id]`, below the MCP access card:

- enable toggle; allowed-domains editor (one origin per line, validated inline with the
  server's error messages); brand color; left/right; title; daily cap;
- the snippet, copyable:
  `<script src="{APP_ORIGIN}/widget.js" data-key="{public_key}" async></script>`;
- a live preview — the real `/embed/{key}` iframe in a device frame, reloaded after save;
- warnings: agent not `active` ("visitors will see nothing until the agent is active");
  enabled with no domains ("only the preview will load"); `create_lead` granted ("anonymous
  visitors can submit leads"); and one line that the domain list stops other websites,
  not direct API use.

Editing requires owner or admin, as for API keys; members see the card read-only.

**GraphQL.** `widgetSettings(agentId): WidgetSettings!` (defaults when no row) and
`updateWidgetSettings(agentId, input): WidgetSettings!` (owner/admin, upsert, origins
normalized server-side). `Agent.publicKey` is added to the GraphQL `Agent` type (it is not exposed today).

**Conversations page — `/dashboard/conversations`.** Agent selector, channel filter
defaulting to `widget` (also `playground`, `api`, all), a list (title, last message time,
message count) and a read-only transcript reusing `ConversationPanel`'s rendering without
the composer. Added to the sidebar. Each lead on `/dashboard/leads` links to
`/dashboard/conversations?conversation={id}`.

`docs/DESIGN.md` is read before this UI work and updated with the widget card, the preview
frame, and the conversations page patterns.

## 8. Testing

**API integration** (`tests/integration/test_widget_*.py`, real Postgres/Redis as today):

- session: new token; refresh keeps `vid`; a token for another agent mints a new `vid`;
- availability: unknown key, draft, disabled, not enabled → identical 404; disabling
  mid-session makes the next message 404;
- tokens: a dashboard access token on widget routes → 401; a widget token on
  `/api/v1/chat/stream` and `/graphql` → 401;
- conversations: visitor B cannot stream into or read visitor A's conversation (404); a
  conversation from another agent → 404; resume returns only the visitor's latest open one;
- projection: a tool-using turn's stream contains no `result`, no `arguments`, no cost;
- limits: each §5 limit, including the daily cap returning `widget_daily_cap`;
- `create_lead` from the widget sets `source='widget'`;
- `frame-policy`: origins only when available;
- RLS on `widget_settings` (added to the isolation tests) and `0016` up/down in
  `test_migrations.py`;
- GraphQL: members cannot update; invalid origins are rejected with a field error.

**API unit:** origin normalization table; `project_public_event` per event; token
encode/decode and `typ` refusal.

**Web:** the frame-ancestors header builder; the snippet builder; the loader's message
filter (a small harness test in the existing web test setup).

**Manual:** a static `infrastructure/widget-demo/index.html` served on another port
(`make widget-demo`), listed as an allowed origin in the seed, to check the whole path in a
real browser.

## 9. Out of scope (YAGNI)

| Not delivered | Why |
|---|---|
| Billing, plans, usage-based limits | Separate Phase 8 slices; the daily cap is a per-agent guard, not a plan limit. |
| Analytics dashboard | Needs its own design; the conversations page is the read surface for now. |
| Conversation retention / deletion policy, visitor data export | Privacy decisions that come with the business accepting real traffic; noted in PHASE-8.md. |
| Human handoff, email notifications on a new lead | Not needed to prove the widget. |
| Proactive greetings, triggers, page-context awareness | Nice to have; the widget does not read the host page. |
| Custom CSS / fonts / avatars | Brand color and position only. |
| Scoring live widget traffic in evaluations | PHASE-6 §9 defers it to after the widget; still deferred. |
| Per-IP trust of `X-Forwarded-For` | Same as login; one proxy-header decision for the whole API, later. |

## 10. Risks

- **The public key is public.** Anyone can script the chat endpoint. Mitigated only by §5;
  a determined abuser with many IPs can spend up to `daily_message_cap` messages per agent
  per day. That cap is the business's worst-case spend control and the card says so.
- **`frame-ancestors` is the only domain check.** Old browsers that ignore CSP Level 2 would
  frame the page anywhere; every current browser honors it. Acceptable.
- **Storage partitioning varies.** Where the iframe's storage is blocked or ephemeral
  (some private modes), resume silently does not work; chat still does.
- **The middleware adds an API round-trip per embed load.** Cached 30 s per key by Next's
  fetch cache; the embed page is loaded once per visitor page view, only when opened.
- **Lifting `_stream_body` out of `api/chat.py`** touches the playground path. The existing
  `test_chat_endpoint.py` suite must pass unchanged.

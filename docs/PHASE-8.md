# Phase 8 — Embeddable chat widget

> Extends [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) §2.2 (the widget's own REST router),
> §2.3 (the one sanctioned pre-tenant lookup, now two), §3.9 (`widget_settings`) and §9.10.
> That document is the binding spec; this one records what Phase 8's first slice adds and
> where it departs from
> [the design](superpowers/specs/2026-09-25-embeddable-widget-design.md) it was built from.
> Billing, plans and analytics — the rest of Phase 8 — are **not started**; see the README
> roadmap.

**Goal:** a business pastes one `<script>` tag into its own site. A visitor, with no
account, opens a chat bubble and talks to that business's agent — the same agent, tools,
knowledge and products the owner tested in the playground.

```text
Business's own site
  <script src="APP/widget.js" data-key="pk_…" async></script>
       │
       ▼
  widget.js  ──draws──▶  launcher + closed shadow root
       │  on open, creates
       ▼
  <iframe src="APP/embed/pk_…">           ← our own origin; CORS never changes
       │
       ├─ POST /api/v1/widget/{key}/session ──▶ mint/refresh a 30-day visitor token
       ├─ GET  /api/v1/widget/conversation  ──▶ resume, or null
       └─ POST /api/v1/widget/chat/stream   ──▶ SSE, same ChatService.send, projected

Domain check: the browser enforces frame-ancestors on the framing site's origin.
The API itself has NO domain check — see §2 and §10.
```

---

## 1. Scope

**In:** a loader script, a public embed page, visitor identity with resume-after-reload, a
public chat stream, per-agent widget settings (enable, allowed domains, appearance, daily
cap), lead capture from the widget, and a dashboard page for reading widget conversations.

**Out:** billing, plans, usage-based limits, an analytics dashboard, conversation
retention/deletion policy, human handoff, proactive greetings, custom CSS/fonts/avatars.
§9 lists these and more, with why.

---

## 2. The one fact every reader deploying this must keep in view

**The allowed-domain list does not protect the API.** It stops *browsers* on other sites
from framing `/embed/{key}` — enforced by `frame-ancestors`, a CSP directive the browser
honours, not the server. It does nothing against a script, `curl`, or a bot calling
`POST /api/v1/widget/chat/stream` directly with a `pk_…` key, because that key is public by
design: it sits in the framing page's own HTML, and nothing about it is a secret (spec §2,
§10). The real abuse controls are §5's rate limits, the per-agent daily cap, and requiring
the agent to be `active` and the widget `enabled`. The dashboard's Website widget card says
this in one line next to the domain editor, and this document says it here first.

A determined abuser with many IPs can still spend up to `daily_message_cap` messages per
agent per day — that cap is the business's actual worst-case spend control, not the domain
list.

---

## 3. Data model

Migration `0016_widget_settings`. `widget_settings` — one row per agent, RLS-scoped like
every tenant table (`app.core.tenancy`, ARCHITECTURE.md §2.3):

| Column | Type | Notes |
|---|---|---|
| `agent_id` | uuid FK → agents, `ON DELETE CASCADE`, unique | |
| `enabled` | bool, default `false` | |
| `allowed_origins` | `text[]`, default `{}`, ≤ 20 | normalized bare origins (see below) |
| `brand_color` | `varchar(7)`, default `#2563eb` | `#rrggbb`, stored lower-case |
| `position` | enum `widget_position` (`right`, `left`) | |
| `title` | `varchar(60)`, nullable | falls back to the agent's name |
| `daily_message_cap` | int, default `500`, check `1..100000` | visitor messages per UTC day |

A missing row means "defaults, disabled" — created on first `updateWidgetSettings`, not
backfilled at agent creation (`app/widget/service.py::_default_view`).

**Origin normalization** (`app/widget/origins.py`, pure, unit-tested): accepts
`https://host[:port]` always, and `http://localhost[:port]` / `http://127.0.0.1[:port]`
only when `ENVIRONMENT=local`. Rejects (never silently strips) a path, query, fragment,
userinfo, wildcard or non-http(s) scheme, so a stored value is exactly what later becomes a
`frame-ancestors` source — nothing downstream re-validates it.

**`resolve_widget(p_public_key text)`** — `SECURITY DEFINER`, `search_path = public,
pg_temp`, `REVOKE ALL FROM PUBLIC`. **Migrations grant `EXECUTE` to whichever role
`DATABASE_URL` names** (`app.db.base.runtime_role()`), the same pattern
`0015_api_keys`'s `resolve_api_key` uses and the same reason `docs/DEPLOYMENT.md` already
documents for it: a literal `app_user` in the migration would grant a role that may not
even exist on a managed database (Neon's roles are named at provisioning time), and a
migrate job whose `DATABASE_URL` names a different role than the literal would leave the
function ungrantable to the role that actually needs it — exactly the failure a production
hotfix (`fix(widget): grant resolve_widget to DATABASE_URL's role, not a literal app_user`)
fixed for `0016` after `0015` hit it first. `docs/DEPLOYMENT.md`'s existing note on
`resolve_api_key` now applies to `resolve_widget` identically.

`ChatService.send` gained `visitor_id: str | None = None`. On an existing conversation, a
mismatched `visitor_id` raises `NotFoundError` — the same answer as a cross-tenant id, so a
guessed or leaked `conversation_id` never reveals whether it belongs to someone else.

`LeadService.create` reads the conversation's `channel` in the same query it already runs
for tenancy and sets `source` to it (`widget`, `playground`, `api`) — no schema change.

---

## 4. Visitor identity and endpoints

`app/api/widget.py`, prefix `/api/v1/widget`. Imports nothing from `app.auth` — a widget
request never carries a dashboard JWT, and a widget token is refused everywhere a dashboard
token is expected (and vice versa), purely by the `typ` claim `decode_token` already checks.

**Visitor token.** HS256 with the existing `JWT_SECRET`, claims `{typ: "widget", org,
agent, vid, iat, exp}`, lifetime `WIDGET_TOKEN_DAYS` (default 30, new env var — see
`.env.example`). `vid` is a random UUID4 string with no link to a person until they
volunteer details to `create_lead`.

| Endpoint | Auth | Does |
|---|---|---|
| `GET /{public_key}/frame-policy` | none | `{allowed_origins}` when available, else `[]`. Always 200. Rate-limited **per public key** (§5). |
| `POST /{public_key}/session` | optional widget bearer | Keeps the bearer's `vid` if it is valid for this agent, else mints a new one. Returns `{token, expires_at, config}`. |
| `GET /conversation` | widget bearer | The visitor's most recent `open` widget conversation, last 50 user/assistant text messages, or `null`. |
| `POST /chat/stream` | widget bearer | `{message (1–2000 chars), conversation_id?}`. Re-checks availability, enforces §5, streams through the same `stream_body` (`app/api/streaming.py`) the dashboard route uses, with §4.1's public event projection. |

**Every bearer-authenticated request re-resolves availability** (`load_available`), so
disabling the widget or the agent takes effect on the visitor's very next message, not when
their 30-day token expires. Unknown key, draft agent, disabled agent and a widget with
`enabled=false` are all the identical `404 {"error": {"code": "not_found", "message":
"widget not found"}}` — the public side cannot distinguish them (`_available_or_404`).

`GET /conversation` reads the last 200 history rows (`_RESUME_SCAN_LIMIT`, four times the
50-message cap) and returns the last 50 that are user/assistant text — the spec states 50
as a cap, not a floor, so a tool-heavy conversation (several extra rows per tool turn) can
legitimately resume with fewer than 50 visible messages. Accepted; a real gap would need a
much larger multiplier or a different query shape entirely.

### 4.1 Public event projection

`app/widget/events.py::project_public_event`, one pure function with a unit test per event:

| Event | Widget receives |
|---|---|
| `message_start` | unchanged |
| `text_delta` | unchanged |
| `tool_call_start` | `{calls: [{name}]}` — names only |
| `tool_call_end` | dropped |
| `citations` | `{citations: [{document_title, page}]}`, deduplicated; product-only citations dropped |
| `message_end` | `{}` |
| `error` | `{code}` plus a fixed public message |

No `arguments`, no `result`, no `excerpt`, no `cost_usd`, no `model` ever reaches a widget
visitor — pinned by an integration test that scans the full SSE body of a tool-using turn
for those keys.

---

## 5. Abuse controls

All through the existing Redis `enforce_rate_limit` (fixed window, fails open and logs).

| Key | Limit | Applies to |
|---|---|---|
| `widget:session:ip:{ip}` | 20 / hour | new-token sessions only (refreshing an existing token is free) |
| `widget:msg:visitor:{vid}` | 10 / minute | chat stream |
| `widget:msg:ip:{ip}` | 30 / minute | chat stream |
| `widget:msg:agent-day:{agent_id}:{yyyymmdd}` | `daily_message_cap` / 86 400 s | chat stream |
| `widget:frame:key:{public_key}` | 120 / minute | frame-policy — **★ per public key, not per IP** |

**★ Departure from the spec, made and recorded during implementation (ledger, Task 5).**
The spec's own text names `widget:frame:ip:{ip}`. That is a defect: the only caller of
`frame-policy` is our own web server's middleware (§6), so a per-IP limit would be **one
shared 120/minute budget for every agent on the platform** — a flood of made-up public
keys (or roughly 60 real agents' embed pages loading inside the same 60-second window)
would exhaust it and unframe every widget at once, including ones nothing is wrong with.
Keying on the public key instead gives each agent, real or made-up, its own budget; a
random-key flood then costs one indexed database lookup per guess with no shared blast
radius, the same exposure §3's `resolve_widget` already accepts for an unknown key. The web
middleware also serves the last known origins for a key when the fetch fails (stale on
error), so one slow or erroring `frame-policy` call never unframes a widget that was
working a moment before.

**Also recorded, not fixed (accepted, see the risk each names):**

- **Unknown-key session probes are not rate-limited.** `POST /{public_key}/session` with a
  key that resolves to nothing costs one indexed database lookup and no LLM spend; the
  session-mint limit only applies once a key resolves to something real. A flood of
  made-up keys is a cheap-but-bounded DB-probe flood, not a free one — no different in kind
  from the frame-policy exposure above. A dedicated failed-lookup limit is a follow-up.
- **The daily cap is consumed by a turn that is later refused** (a bad `conversation_id`,
  or a provider error after the cap check passes) — the cap counts attempts, not
  successes. Bounded, and the alternative (checking the cap only after a turn succeeds)
  would let an attacker probe availability for free before ever being charged against it.
- **Rate limits key on `request.client.host`**, exactly as the auth routes do
  (`app/api/auth.py::_client_key`) — no `X-Forwarded-For` trust. Behind a reverse proxy
  this becomes the proxy's one address for every visitor, the identical, already-documented
  caveat `docs/DEPLOYMENT.md` records for login (see that file's "Before you deploy"
  section, now extended to the widget's own limits).

`create_lead` keeps its existing per-conversation limit (3 / 5 min) and stays off unless
the agent grants it; the dashboard's widget card warns when it is on ("anonymous visitors
can submit leads").

**Privacy.** No IP is stored — it lives only inside a rate-limit key that expires.
`visitor_id`/message text never appear in logs. A widget session rejection logs only the
key's first 8 characters (`app/api/widget.py::_KEY_LOG_PREFIX`), and `app/main.py`'s
request-logging middleware truncates a widget public key inside any request path to the
same 8 characters before it ever reaches a log line
(`_WIDGET_KEY_IN_PATH`/`_loggable_path`).

**uvicorn's own access log is disabled** (`--no-access-log`, in `apps/api/Dockerfile`'s
`CMD` and `docker-compose.yml`'s `api` command) **because it would otherwise print a second,
unredacted copy of every request** — the client IP and the full public key in the path, in
plain text, duplicating exactly what the app's own middleware already logs correctly with
the IP omitted and the key truncated. `render.yaml` has no start command of its own (Render
runs the Dockerfile's `CMD` as-is), so no separate change was needed there;
`docs/DEPLOYMENT.md` records the same one-line reasoning.

---

## 6. Web: loader and embed page

**Loader — `apps/web/public/widget.js`.** Hand-written ES2017, no build step, no
dependencies, ~4.7 KB stripped / ~2.4 KB gzipped (the spec's target was "< 5 KB"; the
shipped file is 6.0 KB raw, before minification — a follow-up if raw size ever matters more
than what actually reaches the wire). Draws a launcher and, on first open, an iframe onto
`APP_ORIGIN/embed/{key}` inside a **closed** shadow root, so host CSS/scripts cannot reach
in and the widget's cannot reach out. Accepts `message` events only from the iframe's own
`contentWindow` at `APP_ORIGIN`, and posts only `{type: "open"}` back, targeted at that
origin.

**Not delivered: the spec's `unread {count}` loader message.** Nothing in the embed page
produces an unread count yet — there was no use for it (the widget has no closed-tab
notion of "new since you left"), so it was left unbuilt rather than stubbed. Adding it is a
follow-up, not a partial implementation left half-wired.

**Embed page — `apps/web/src/app/embed/[publicKey]/page.tsx`.** Its own minimal layout —
**no `AuthProvider`, no urql** — because the root layout wraps every other route in both,
and a public, unauthenticated page must not carry either (this required moving those
providers out of the root layout into the `dashboard` and `(auth)` layouts specifically,
beyond what the spec called out, so a provider a visitor's session cannot use is never
mounted on their page).

Stores its resume token in `localStorage["widget:{publicKey}"]`, inside try/catch. **This
is the one documented exception to `docs/DESIGN.md`'s rule against persisting client state
in browser storage** — accepted because it is genuinely per-visitor, per-site state (not
dashboard data), a lost token only costs a fresh conversation rather than anything a
business relies on, and it is exactly the mechanism the spec's Approach A calls for
(§5/"Approach in one line" of the design doc): storage partitioned per top-level site by
modern browsers is what makes "one visitor identity per business site" the right meaning
at all.

**Not addressed: contrast on a light `brand_color`.** The launcher/button text is white
regardless of the owner's chosen brand color; a very light color (near-white) can fail
contrast against white text. No luminance-based text-color switch was added. Left for a
follow-up rather than guessed at without a design pass.

**Frame policy — `src/middleware.ts` + `src/lib/frame-policy.ts`.** For `/embed/:path*`
only, fetches `GET /api/v1/widget/{key}/frame-policy` server-side (through
`auth-proxy.ts`'s `proxyTargetUrl`), caches each key's answer for 30 seconds in a
module-level map (the edge runtime has no access to Next's own fetch cache), and sets
`Content-Security-Policy: frame-ancestors 'self' <origins…>` while removing
`X-Frame-Options` for that route only. A failed fetch (network, timeout, non-200 including
429) serves the key's last successful answer past its TTL rather than failing closed — one
blip in our own API must not unframe every live widget. With no origins, or no earlier
success at all, `frame-ancestors 'self'` only — the dashboard's own preview iframe still
loads; every other site is refused.

`parseSSEStream`'s frame reader is split out as `parseSSEFrames(chunks, toEvent)` so the
widget can pass its own `toWidgetEvent` without duplicating frame parsing; the playground's
own `parseSSEStream` is unchanged in behaviour.

---

## 7. Dashboard

**Website widget card** (`src/components/agents/WidgetCard.tsx`) on `/dashboard/agents/[id]`,
below the MCP access card: enable toggle, allowed-domains editor (one origin per line,
server error messages surfaced inline), brand color, left/right, title, daily cap, the
copy-ready `<script src="{APP_ORIGIN}/widget.js" data-key="{public_key}" async></script>`
snippet, and a live `/embed/{key}` preview reloaded after save. Warnings: agent not
`active`; enabled with no domains; `create_lead` granted; and the one-line reminder from §2
that the domain list stops other websites, not direct API use. Editing requires owner or
admin, same as API keys; members see the card read-only.

**GraphQL.** `widgetSettings(agentId): WidgetSettings!` (defaults when no row),
`updateWidgetSettings(agentId, input): WidgetSettings!` (owner/admin, upsert, origins
normalized server-side). `Agent.publicKey` was added to the GraphQL `Agent` type — it
existed on the model since Phase 1 but nothing exposed it before this phase.

**Conversations page — `/dashboard/conversations`.** Agent selector, channel filter
(defaulting to `widget`; also `playground`, `api`, all), a list (title, last message time,
message count), a read-only transcript reusing `ConversationPanel`'s rendering without the
composer. Each lead on `/dashboard/leads` links to
`/dashboard/conversations?conversation={id}` — a once-only guard keeps a later manual
navigation on the page from re-triggering that jump.

---

## 8. Testing

Backend integration (`tests/integration/test_widget_*.py`, real Postgres/Redis): session
mint/refresh/vid-reuse-across-agents; availability (unknown key, draft, disabled, not
enabled → identical 404; disabling mid-session → next message 404); token cross-use (widget
token on `/chat/stream` or `/graphql` → 401; dashboard token on widget routes → 401);
cross-visitor conversation isolation (404, not a leak); the projection (a tool-using turn's
full SSE body has no `result`, `arguments`, or cost); every §5 limit including the daily
cap's `widget_daily_cap` code; `create_lead` from the widget sets `source='widget'`;
frame-policy origins only when available; RLS on `widget_settings`; `0016` up/down.

Unit: origin normalization table; `project_public_event` per event; token encode/decode and
`typ` refusal; `tests/integration/test_seed.py` (Task 7) — the seed enables the demo
widget for `http://localhost:5500`, activates the demo agent (a freshly created agent
starts `draft`, and `load_available` refuses a draft agent identically to an unknown key —
enabling `widget_settings` alone is not enough for the demo to actually work), asserts
`load_available` returns a `PublicWidget` end to end, and re-running it does not drift from
those values.

Web: the frame-ancestors header builder; the snippet builder; the loader's message filter;
`WidgetChat`/embed-page behaviour; the Conversations page's once-only `?conversation=`
guard.

Manual: `infrastructure/widget-demo/index.html`, a static fake storefront served on its own
origin (`make widget-demo` → `python -m http.server 5500`), listed as the demo agent's one
allowed origin by the seed, which prints the ready-to-open demo URL.

---

## 9. Not delivered

Copied from the spec's own §9, plus what surfaced during implementation.

| Not delivered | Why |
|---|---|
| Billing, plans, usage-based limits | Separate Phase 8 slices; the daily cap is a per-agent guard, not a plan limit. |
| Analytics dashboard | Needs its own design; the Conversations page is the read surface for now. |
| Conversation retention / deletion policy, visitor data export | Privacy decisions that come with the business accepting real traffic. |
| Human handoff, email notifications on a new lead | Not needed to prove the widget. |
| Proactive greetings, triggers, page-context awareness | The widget does not read the host page. |
| Custom CSS / fonts / avatars | Brand color and position only. |
| Scoring live widget traffic in evaluations | PHASE-6 §9 defers it to after the widget; still deferred. |
| Per-IP trust of `X-Forwarded-For` | Same as login; one proxy-header decision for the whole API, later. |
| The spec's `unread {count}` loader message | §6 — nothing produces an unread count yet; no use for it without a closed-tab notion of "new since you left". |
| Luminance-based launcher text color | §6 — a light `brand_color` can fail contrast against the fixed white button text; not addressed. |
| A rate limit on unknown-key session probes | §5 — bounded to one DB lookup per guess today; a dedicated failed-lookup limit is a follow-up. |
| A rate limit distinguishing a refused turn from a billed one | §5 — the daily cap is consumed before a turn is known to succeed. |

---

## 10. Risks

- **The public key is public, and the domain list does not change that** (§2). Mitigated
  only by §5's limits and the daily cap, which is the business's actual worst-case spend
  control.
- **`frame-ancestors` is the only domain check**; a browser that ignores CSP Level 2 would
  frame the page anywhere. Every current browser honours it.
- **Storage partitioning varies.** Where the iframe's `localStorage` is blocked or
  ephemeral (some private modes), resume silently does not work; chat still does.
- **The frame-policy middleware adds one API round-trip per embed load**, cached 30
  seconds per key with stale-on-error, so a transient API blip does not unframe a working
  widget.
- **Lifting the streaming body out of `api/chat.py`** (`app/api/streaming.py::stream_body`)
  touches the playground path; `test_chat_endpoint.py` passes unchanged, pinning that.

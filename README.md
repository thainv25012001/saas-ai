# AI Sales Agent

A multi-tenant SaaS where a business configures an AI sales assistant over its own
knowledge — products, documents, prompts — and that assistant talks to the business's
customers. This repository is **Phases 1 and 2**: authentication, organizations, agents
and prompts, plus a real LLM call — a provider abstraction (OpenAI, Anthropic, and a
network-free `fake`), conversations and messages, a streaming `POST /api/v1/chat/stream`
endpoint with per-message token and cost accounting, and a working playground. Later
phases (retrieval, tool-calling, evaluation, MCP, billing) build on this foundation; see
[Phase roadmap](#phase-roadmap) below.

## Architecture, in one picture

```text
┌──────────────────────────────────────────────────────────────────┐
│  apps/web  ·  Next.js (App Router, TS, Tailwind)                 │
│                                                                  │
│   Dashboard  ──GraphQL (urql)──┐        Playground / Widget      │
│                                │              │                  │
└────────────────────────────────┼──────────────┼──────────────────┘
                                 │              │ SSE (fetch stream)
┌────────────────────────────────┼──────────────┼──────────────────┐
│  apps/api  ·  FastAPI (modular monolith)                         │
│                                ▼              ▼                  │
│   ┌────────────────┐   ┌──────────────┐  ┌───────────────────┐   │
│   │ GraphQL layer  │   │  REST: auth  │  │ REST: /chat/stream│   │
│   │  (Strawberry)  │   │  + uploads   │  │      (SSE)        │   │
│   └───────┬────────┘   └──────┬───────┘  └────────┬──────────┘   │
│           │                   │                   │              │
│           └───────────┬───────┴───────────────────┘              │
│                       ▼                                          │
│   ┌──────────────────────────────────────────────────────────┐   │
│   │  Service layer  (org-scoped; no HTTP or LLM types leak)  │   │
│   │  agents · documents · products · conversations ·         │   │
│   │  prompts · leads · evaluations                           │   │
│   └───────┬──────────────────────────────┬───────────────────┘   │
│           │                              │                       │
│   ┌───────▼────────────┐        ┌────────▼──────────┐            │
│   │ Agent Orchestrator │───────▶│   Tool Registry   │            │
│   │  (later phases)    │◀───────│   (later phases)   │            │
│   └───────┬────────────┘        └────────┬──────────┘            │
│           │                              │                       │
│   ┌───────▼──────────┐  ┌────────────────▼───────┐               │
│   │  LLM Provider    │  │  RAG (retrieval)       │               │
│   │  (later phases)  │  │  (later phases)        │               │
│   └──────────────────┘  └────────────────────────┘               │
│                                                                  │
│   Repository layer (async SQLAlchemy 2.0, tenant-bound session)   │
└───────────┬─────────────────────────────┬────────────────────────┘
            │                             │
   ┌────────▼─────────┐          ┌────────▼────────┐
   │ PostgreSQL 16    │          │  Redis          │
   │ + pgvector       │          │  cache · rate   │
   │ + RLS · tsvector │          │  limit          │
   └──────────────────┘          └─────────────────┘
```

The full rationale — why GraphQL *and* SSE, why row-level security instead of relying on
application code alone, why prompts are versioned data instead of a constant in the
codebase — lives in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), not repeated here.

## Quick start

Requires Docker and Docker Compose. Nothing else needs to be installed to get the
stack running.

```bash
cp .env.example .env
docker compose up -d --wait
docker compose exec -T api uv run python -m app.db.seed
```

`--wait` is what makes this safe to run as a two-command block: it blocks until every
service reports healthy (`api`'s healthcheck only passes once its migrations have
finished and `uvicorn` has started) before handing control back, instead of returning as
soon as the containers are merely *started*. Without it, the seed command can race the
API's own startup and fail with "relation does not exist" — the migrations genuinely
haven't run yet. `uv` already lives inside the `api` image, so `docker compose exec` runs
the seed script through the container — no native Python/`uv` install needed on the host
for this path. To watch what's happening instead, drop `--wait` and use
`docker compose up` in the foreground in one terminal and the seed command in a second,
once you see `Application startup complete` in the logs. Once it's running:

```bash
docker compose logs -f     # follow logs from all four services
docker compose down        # stop everything
```

Then open [http://localhost:3000](http://localhost:3000) and sign in with the seeded
demo account:

- **Email:** `demo@example.com`
- **Password:** `demo-password-123`

This gives you an OWNER membership in the seeded "Demo Motors" organization, with one
demo agent already created. From there: register a second account, log in, create an
agent, edit it, reload the page — the whole Phase 1 loop.

No manual step is required beyond copying `.env.example`. `docker compose up` builds the
`api` and `web` images, starts Postgres and Redis, waits for both to report healthy,
applies every Alembic migration, and then starts the API (whose own healthcheck hits
`/health/ready`, so it only reports healthy once migrations are done) and the dashboard.

> **Production note:** the `api` container runs `alembic upgrade head` as part of its own
> startup command (see `docker-compose.yml`). That is exactly right for a single-instance
> local stack — clone, compose up, schema is always current — and wrong for a real
> deployment: with more than one replica, every replica would race to apply the same
> migration at once. In production, run migrations as a separate one-shot step before new
> replicas start, not inside each replica's entrypoint.

### Windows, or anywhere without `make`

This machine's Makefile targets GNU `make`, which some Windows installs don't have. The
Makefile is still the source of truth for CI and for Linux/macOS/WSL — the table below
gives the exact command each target runs, so nothing is out of reach without it.

## Repository layout

```text
apps/
  api/            FastAPI backend (Python, uv, SQLAlchemy 2.0, Alembic, Strawberry GraphQL)
    app/
      core/       config, structured logging, typed errors, security, tenancy, rate limiting
      db/         models and the async session/engine
      auth/       registration, login, refresh rotation
      agents/     agent + agent-config service and schemas
      prompts/    prompt + prompt-version service (versioning, single-active invariant)
      llm/        provider abstraction: openai, anthropic, fake; capabilities, pricing, registry
      chat/       the chat service: prompt resolution, history window, streaming, persistence
      conversations/  conversation + message + usage-event service
      graphql/    Strawberry schema, context, resolvers
      api/        REST routers: auth, health, chat (SSE)
    alembic/      migrations (RLS policies land here, not in application code)
    tests/        unit + integration (incl. the cross-tenant isolation and RLS suites)
  web/            Next.js 15 dashboard (App Router, TypeScript strict, Tailwind v4, urql)
    src/
      app/        routed pages: (auth)/login, (auth)/register, dashboard/...
      graphql/    .graphql documents + committed codegen output (generated.ts)
      lib/        GraphQL client, auth/token handling
packages/shared/  schema.graphql — the committed GraphQL SDL both sides build against
infrastructure/
  postgres/       init.sql — extensions (vector, citext, pgcrypto) and the app_owner/app_user roles
  scripts/        verify_db.sh — asserts the roles/extensions/RLS setup is actually correct
docs/
  ARCHITECTURE.md the full design document — read this for "why", not just "what"
docker-compose.yml
.env.example
Makefile
```

## Every `make` target

Run from the repository root. Each row also gives the direct command, for a machine
without `make`.

| Target | What it does | Without `make` |
|---|---|---|
| `make up` | Starts `db` and `redis` only (detached) — useful when running the API/web natively instead of in containers. | `docker compose up -d db redis` |
| `make down` | Stops every compose service. | `docker compose down` |
| `make logs` | Follows logs for all compose services. | `docker compose logs -f` |
| `make verify-db` | Asserts the Postgres extensions, `app_owner`/`app_user` roles, and RLS configuration are correct. Requires `db`/`redis` to be up. Needs a `bash` shell (Git Bash/WSL on Windows). | `bash infrastructure/scripts/verify_db.sh` |
| `make api` | Runs the API natively (not in a container) with autoreload, against whatever `DATABASE_URL`/`REDIS_URL` your shell has set (e.g. from `make up`). | `cd apps/api && uv run uvicorn app.main:app --reload --port 8000` |
| `make test` | Runs the backend test suite (281 tests: unit + integration, incl. tenant isolation, RLS, and the chat/streaming suites). | `cd apps/api && uv run pytest -v` |
| `make lint` | Backend lint/format/type gate: `ruff check`, `ruff format --check`, `mypy --strict`. | `cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app/` |
| `make migrate` | Applies every Alembic migration up to head. | `cd apps/api && uv run alembic upgrade head` |
| `make revision m="message"` | Creates a new Alembic revision with an autogenerated diff. | `cd apps/api && uv run alembic revision -m "message"` |
| `make schema` | Exports the live GraphQL SDL to `packages/shared/schema.graphql` (the contract the web app's codegen reads). | `cd apps/api && uv run strawberry export-schema app.graphql.schema:schema > ../../packages/shared/schema.graphql` |
| `make seed` | Creates the demo organization/user/agent/prompt (idempotent). **Refuses to run unless `ENVIRONMENT=local`** — see [Before you deploy](#before-you-deploy). Native: needs `uv`/Python on the host, and `DATABASE_URL`/`REDIS_URL` pointed at a running `db`/`redis` (e.g. from `make up`). Docker-only (no host `uv` needed, and what the Quick Start above uses): `docker compose exec -T api uv run python -m app.db.seed`. | `cd apps/api && uv run python -m app.db.seed` |
| `make web` | Runs the Next.js dev server natively. | `cd apps/web && npm run dev` |
| `make web-codegen` | Regenerates `src/graphql/generated.ts` from `packages/shared/schema.graphql`. | `cd apps/web && npm run codegen` |
| `make web-typecheck` | `tsc --noEmit` on the web app. | `cd apps/web && npm run typecheck` |
| `make web-lint` | ESLint on the web app. | `cd apps/web && npm run lint` |

## Running tests and linters

Backend, against `db`/`redis` from `make up` (or the full compose stack):

```bash
make lint && make test
# without make:
cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app/
cd apps/api && uv run pytest -v
```

Frontend:

```bash
cd apps/web && npm run test && npm run typecheck && npm run lint && npm run build
```

`npm run test` is vitest (22 tests), covering the SSE frame parser, the chat stream's
token-refresh path, and the playground's conversation-identity state machine. It starts
no server and makes no network call.

CI (`.github/workflows/ci.yml`) runs both, plus a check that `packages/shared/schema.graphql`
and `apps/web/src/graphql/generated.ts` are both up to date with the code that generates
them, on every push to `main` and every pull request. The web job never starts the API —
it typechecks against the committed schema and committed generated types, so a backend
outage or a slow database never blocks a frontend-only PR.

## Phase roadmap

| Phase | Scope | Status |
|---|---|---|
| **1 — Foundation** | Repo structure, FastAPI, GraphQL, Postgres + RLS, migrations, auth, organizations, users, agents, basic Next.js dashboard. | **Complete** (this repository) |
| **2 — Basic LLM chat** | Next.js → chat API → LLM provider → streaming response. OpenAI first, then an Anthropic adapter behind the same interface. | **Complete** (this repository) — see [`docs/PHASE-2.md`](docs/PHASE-2.md) |
| 3 — RAG | Document upload → extraction → chunking → embedding → pgvector → retrieval → LLM. | Not started |
| 4 — Agent + tools | The agent decides when to call `retrieve_knowledge`, `search_products`, `get_product`, `create_lead`. | Not started |
| 5 — Evaluation | Test datasets, evaluation runs, retrieval and answer scoring. | Not started |
| 6 — MCP | Expose selected business capabilities through MCP, once the built-in tool system is stable. | Not started |
| 7 — SaaS features | Billing/Stripe, usage limits, subscription plans, embeddable widget, analytics, lead dashboard. | Not started |

## Before you deploy

Two settings that are safe defaults for local development and become security decisions
the moment this leaves your laptop:

- **`ENVIRONMENT` defaults to `local`.** While it is `local`, the GraphQL IDE (GraphiQL)
  is served at `/graphql`, GraphQL introspection is enabled, refresh cookies are not
  marked `Secure`, and `make seed` (which creates an OWNER account with a password
  committed to this repo) is allowed to run. **Set `ENVIRONMENT` to anything else — e.g.
  `production` — in any environment reachable by someone other than you.** A deployment
  that forgets this exposes the GraphQL IDE and schema introspection publicly, and would
  let the seed script re-create `demo@example.com` / `demo-password-123` as an owner if
  someone ran it against that database.
- **Auth rate limiting is keyed on `request.client.host`** (see `apps/api/app/api/auth.py`).
  That is the direct TCP peer's address, which is correct with no proxy in front of the
  API — exactly Phase 1's setup. Behind a load balancer, reverse proxy, or ingress, every
  client collapses into that proxy's single IP, so five registrations per hour becomes a
  global limit one attacker can exhaust for everyone. Naively trusting
  `X-Forwarded-For` is not the fix — it lets an attacker mint a fresh rate-limit key on
  every request by forging the header. Before deploying behind a proxy, configure
  Starlette/uvicorn's `ProxyHeadersMiddleware` with an explicit trusted-hosts list (or run
  uvicorn with `--proxy-headers --forwarded-allow-ips=<the proxy's real address>`) so only
  a header set by that trusted hop is honored.

- **The SSE route must not be buffered or compressed by anything in front of it.**
  `POST /api/v1/chat/stream` sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`,
  and sends no `Content-Encoding` (see `docs/PHASE-2.md` §4). Those headers only work if
  the proxy in front of the API honours them: nginx needs `proxy_buffering off;` (or the
  `X-Accel-Buffering` header respected, which it is by default) and `gzip off;` on that
  location, and any CDN or ingress in the path needs response buffering and compression
  disabled for it too. A proxy that buffers or gzips this route does not fail loudly — it
  holds the whole response and delivers it as one blob at the end, so streaming silently
  degrades to a long pause followed by the complete answer, which looks like a slow model
  rather than a misconfigured proxy. Also disable any idle-connection timeout shorter
  than the `: ping` heartbeat interval (15 s).

## Design decisions

Rationale for the choices that most shape this codebase lives in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md):

- **Two-layer tenant isolation** — application-level `TenantContext` plus Postgres
  row-level security as a second, structural layer that doesn't depend on every query
  remembering a `WHERE organization_id = ...` — §2.3.
- **GraphQL for the dashboard, plain SSE for token streaming** — GraphQL subscriptions
  would need a second transport (WebSocket), a second auth path, and sticky sessions, all
  to deliver a one-way byte stream that `fetch()` already handles — §2.2.
- **Prompts are versioned data, not a constant in the codebase** — pulled forward into
  Phase 1 specifically so "don't hard-code the system prompt" holds from the first LLM
  call in Phase 2 onward — §9.2.

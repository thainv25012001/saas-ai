# Deployment

| Piece | Platform | How it ships |
| --- | --- | --- |
| Web (Next.js) | Vercel | `vercel build` + `vercel deploy --prebuilt` from CI |
| API (FastAPI) | Render | Docker service, deploy triggered through the Render API |
| Postgres | Neon | Migrations run from CI as `app_owner` |
| Redis | Upstash | TLS URL in the API's environment |
| CI/CD | GitHub Actions | `.github/workflows/ci.yml`, `.github/workflows/deploy.yml` |

## How the pipeline runs

`ci.yml` runs on every pull request and every push to `main`:

- **api** — provisions a throwaway Postgres + Redis, applies `infrastructure/postgres/init.sql`,
  then `ruff check`, `ruff format --check`, `mypy`, `alembic upgrade head`, `pytest`, and a check
  that `packages/shared/schema.graphql` matches what the code exports.
- **web** — `npm ci`, a check that `src/graphql/generated.ts` matches codegen output, `eslint`,
  `tsc --noEmit`, and `next build`.

`deploy.yml` starts only when CI concludes successfully on `main` (or when dispatched by hand),
and runs three jobs in order:

1. **migrate** — `alembic upgrade head` against Neon, once, from the runner. The API container
   deliberately does not migrate on boot: that would run once per replica.
2. **api** — asks Render for a deploy, polls until it is `live`, then requires `/health/ready` to
   report `ready`, which proves the new container can reach Neon and Upstash.
3. **web** — builds the Next.js app on the runner and uploads it to Vercel, so the exact commit CI
   verified is the one that ships.

Every job uses the `production` GitHub environment, so adding a required reviewer there turns the
whole thing into a gated release.

## One-time setup

### 1. Neon

Create a project, then run this as the Neon owner role (SQL Editor). It is
`infrastructure/postgres/init.sql` with real passwords and Neon's database name:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE ROLE app_owner WITH LOGIN PASSWORD '<owner-password>' NOBYPASSRLS;
CREATE ROLE app_user  WITH LOGIN PASSWORD '<user-password>'  NOBYPASSRLS;

GRANT CONNECT ON DATABASE neondb TO app_owner, app_user;
GRANT USAGE ON SCHEMA public TO app_owner, app_user;
GRANT CREATE ON SCHEMA public TO app_owner;

ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
```

Run this **before the first deploy**. `ALTER DEFAULT PRIVILEGES` only applies to tables created
after it, so if migrations run first, `app_user` ends up with no privileges at all and the API
answers every request with `permission denied for table organizations` while the migrate job
reports success. If that already happened, grant the existing tables directly (as `app_owner`):

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user;
```

The `citext` extension is also created by migration `0001` (`CREATE EXTENSION IF NOT EXISTS`), so a
database that is bare but whose migration role may create extensions provisions itself. On Neon a
role created with plain `CREATE ROLE` usually may not, which is why the statement above stays in the
bootstrap: skipping it fails the migrate job with `type "citext" does not exist`.

The role split is what makes Row-Level Security real: `app_owner` owns the tables (and so bypasses
RLS) and only runs migrations; `app_user` is what the application connects as and is subject to
every policy. Neither role has `BYPASSRLS`.

**Migrations grant to whichever role `DATABASE_URL` names.** Migration `0015_api_keys` creates the
`resolve_api_key` `SECURITY DEFINER` function (MCP key lookup) and grants EXECUTE on it to the
runtime role, read from `DATABASE_URL`'s user (`app.db.base.runtime_role`) — so the migrate job must
be given the same `DATABASE_URL` the API uses, and that role must already exist when migrations run.
`app_user` is only the name this guide and local development use. Renaming the role later leaves it
without that grant; re-run the `GRANT EXECUTE ON FUNCTION resolve_api_key(bytea) TO <role>` by hand.
Migration `0016_widget_settings` (Phase 8, the widget's `resolve_widget` public-key lookup) grants the
same way, to the same role, for the same reason — a literal `app_user` in the migration would grant a
role a managed database may not even have. If you rename the role, also re-run
`GRANT EXECUTE ON FUNCTION resolve_widget(text) TO <role>`.

Paste the connection strings Neon gives you as they are, only swapping in the `app_user` /
`app_owner` credentials. `Settings` normalizes them on load (`_normalize_database_url` in
`apps/api/app/core/config.py`): a driver-less `postgresql://` or legacy `postgres://` scheme becomes
`postgresql+asyncpg://`, and libpq's `sslmode`/`channel_binding` become asyncpg's `ssl`. Without
that, a driver-less scheme makes SQLAlchemy load psycopg2 and the container dies at import with
`ModuleNotFoundError: No module named 'psycopg2'`, which points nowhere near the real cause.

What the app ends up using:

```
postgresql+asyncpg://app_user:<user-password>@<host>/neondb?ssl=require
postgresql+asyncpg://app_owner:<owner-password>@<host>/neondb?ssl=require
```

`ssl=require` encrypts the connection but does not verify Neon's certificate. Set
`sslmode=verify-full` (or `ssl=verify-full`) for that, once the runtime has a CA bundle you trust.

Use Neon's pooled host for `DATABASE_URL` and the direct (unpooled) host for
`MIGRATION_DATABASE_URL`; DDL through a transaction pooler is a bad time.

### 2. Upstash

Create a Redis database and take the TLS URL: `rediss://default:<password>@<host>:6379`. That is
`REDIS_URL`. `redis-py` handles the `rediss://` scheme with no code change.

### 3. Render

Import `render.yaml` as a Blueprint, then fill in the environment variables it leaves blank
(`DATABASE_URL`, `MIGRATION_DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`, `MCP_ALLOWED_HOSTS`,
`WIDGET_FRAME_POLICY_SECRET` — see "The embeddable widget" below).
`JWT_SECRET` is generated by Render — copy it into the GitHub secret of the same name so the
migrate job and the API agree. Set `CORS_ORIGINS` to the Vercel production origin, e.g.
`https://saas-ai.vercel.app`.

Set `MCP_ALLOWED_HOSTS` to the API's own public hostname, e.g. `saas-ai-api.onrender.com:*` (or
your custom domain). The MCP endpoint (`/mcp`) checks every request's `Host` header against this
comma-separated list and answers anything else **421**; the default is loopback only, so an unset
value refuses every real MCP call, and startup logs `mcp_allowed_hosts_loopback_only` as a warning
when `ENVIRONMENT=production`. A bare entry (`saas-ai-api.onrender.com`) matches only that exact
`Host`; `host:*` matches the host with any explicit port **and** — expanded by the setting's parser,
because the SDK's own `:*` requires a port — the bare host an HTTPS client on 443 actually sends. So
`saas-ai-api.onrender.com:*` alone is enough.

`/mcp` needs no CORS for its intended callers: MCP clients are server-to-server and send no
`Origin`, which is admitted. A request that *does* carry a browser `Origin` must match
`CORS_ORIGINS`, or it gets **403**.

`autoDeploy` is off on purpose: deploys come from CI after the tests pass and the migration lands.
From the service's dashboard URL, take the `srv-…` id (`RENDER_SERVICE_ID`) and create an API key
under Account Settings → API Keys (`RENDER_API_KEY`).

Render's free instances spin down when idle, so the first request after a quiet period is slow.
That includes MCP: a client's first call after a spin-down can time out while the instance cold
starts; retrying once it is up succeeds.

Only one service is deployed, so the arq worker runs *inside* the API process
(`RUN_EMBEDDED_WORKER=true`). This is not the shape docker-compose uses, and the reason is the
uploaded bytes: `app/rag/storage.py` writes them to a local directory, a Render Disk attaches to
exactly one service, and a separate worker service would therefore never see them. Deploying the
API on its own is worse — uploads are accepted, the job is queued, nothing consumes it, and every
document stays `pending`.

What that costs, and how to tell:

- Ingestion shares one CPU and one connection pool with request handling. `WORKER_MAX_JOBS` is set
  to 2 here for that reason, against the standalone worker's 5.
- Uploads do not survive a deploy or a spin-down. Already-ingested chunks live in Postgres and keep
  answering; re-ingesting an older document is what fails.
- A restart cancels an in-flight ingest instead of draining it. arq re-queues the job once its
  in-progress key expires, so it recovers by itself — but the document reads `processing` for the
  ~10 minutes that takes.
- Startup logs `embedded_worker_started`. If Redis is unreachable the app still serves HTTP and
  logs `embedded_worker_stopped` with the error, rather than crash-looping.

Moving to a dedicated worker means backing `storage.py` with object storage, then setting
`RUN_EMBEDDED_WORKER=false` and adding a `type: worker` service running
`uv run arq app.workers.settings.WorkerSettings`.

### 4. Vercel

Create the project and, because this workflow builds and uploads the app itself:

- Leave the project's **Root Directory** empty — the CLI runs from `apps/web`, which already *is*
  the app root. (Setting it to `apps/web` as well makes Vercel look for `apps/web/apps/web`.)
- Turn off the Git integration's automatic deploys (Settings → Git), or every push deploys twice,
  once unverified.
- Add `NEXT_PUBLIC_API_URL` to the Production environment, pointing at the Render URL. `vercel pull`
  brings it into the CI build. It has to be present *at build time*, not just at runtime: the
  rewrite that proxies the auth routes is baked into the build (see below).
- Add `WIDGET_FRAME_POLICY_SECRET` to the Production environment with the **same value** as the
  Render service's. It is server-only (never `NEXT_PUBLIC_`); the middleware reads it at runtime.

The auth routes — and only those — are served from the Vercel origin and proxied to Render, via the
rewrite in `next.config.ts` (`src/lib/auth-proxy.ts` has the rules and the full reasoning). The
refresh token is an httpOnly cookie, and `saas-ai.vercel.app` and `saas-ai-api.onrender.com` are
different registrable domains that cannot share one. Calling the auth routes cross-origin therefore
left the cookie unreadable by `middleware.ts` and unsent by the browser, so a successful login
landed straight back on the login form. Do not "simplify" those calls back onto `NEXT_PUBLIC_API_URL`.

GraphQL and the SSE chat stream still go to Render directly on a Bearer token, which is why
`CORS_ORIGINS` above is still required.

Take `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` from `.vercel/project.json` after a local
`vercel link`, and create `VERCEL_TOKEN` under Account Settings → Tokens.

### 5. GitHub

Create an environment named `production` (Settings → Environments) and add:

| Secret | Value |
| --- | --- |
| `DATABASE_URL` | Neon pooled URL as `app_user`, `?ssl=require` |
| `MIGRATION_DATABASE_URL` | Neon direct URL as `app_owner`, `?ssl=require` |
| `REDIS_URL` | Upstash `rediss://` URL |
| `JWT_SECRET` | Same value as the Render service |
| `RENDER_API_KEY` | Render account API key |
| `RENDER_SERVICE_ID` | `srv-…` |
| `VERCEL_TOKEN` | Vercel account token |
| `VERCEL_ORG_ID` | From `.vercel/project.json` |
| `VERCEL_PROJECT_ID` | From `.vercel/project.json` |

| Variable | Value |
| --- | --- |
| `API_URL` | Public Render URL, no trailing slash, e.g. `https://saas-ai-api.onrender.com` |

`deploy.yml` only fires for a `CI` workflow that exists on the default branch, so nothing deploys
until both workflow files are merged to `main`.

## The embeddable widget (Phase 8)

Three things a reverse proxy or CDN in front of either app must get right, beyond what
already applies to the rest of the API:

- **The embed route must not be framed-blocked by a proxy adding its own
  `X-Frame-Options` or `Content-Security-Policy: frame-ancestors`.** `apps/web/src/
  middleware.ts` sets `frame-ancestors` for `/embed/*` from the agent's own allowed
  origins and deliberately removes `X-Frame-Options` there — a CDN or edge proxy that adds
  either header of its own on top overrides or duplicates what the app already computed
  correctly, and a widget that should be framable becomes refused everywhere. Vercel's edge
  network does not do this by default; check first if you put anything else in front of it.
- **`/api/v1/widget/*` needs the same SSE proxy settings as `/api/v1/chat/stream`.** The
  widget's own `chat/stream` route streams `text/event-stream` exactly like the playground's
  (see "Before you deploy" below) — the same `proxy_buffering off;`/`gzip off;` (nginx) or
  equivalent, and the same longer-than-15-second idle timeout, apply to it too. A proxy that
  buffers this route does not fail loudly; it just turns every widget reply into one long
  pause followed by a complete answer.
- **The per-IP rate limits need the real client address.** `app/core/request.py::client_ip`
  reads `request.client.host`, so behind a proxy every visitor would collapse into the proxy's
  one address and the per-IP limits in `docs/PHASE-8.md` §5 (30 messages/minute, 20 new
  sessions/hour, and login's) would become one shared budget for the whole platform. The
  image therefore runs uvicorn with `--proxy-headers
  --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"`, and `render.yaml` sets
  `FORWARDED_ALLOW_IPS='*'`. **`'*'` is only safe when the container is reachable solely
  through a trusted proxy**, as a Render web service is: anyone who can reach the container
  directly could otherwise forge `X-Forwarded-For` and mint a fresh rate-limit key per
  request. Anywhere else, set it to the proxy's own address (or leave the loopback default
  when nothing sits in front). docker-compose leaves it unset. The per-visitor-token limit
  (10/minute) and the per-agent daily cap do not depend on the client IP either way.
- **Set `WIDGET_FRAME_POLICY_SECRET` to the same value on both services** (Render and
  Vercel; generate it like `JWT_SECRET`). The web middleware sends it as
  `X-Widget-Frame-Secret` when it asks the API which sites may frame a widget, and a matching
  header skips that route's per-key rate limit. Left unset, the limit applies to the
  middleware too, and because a public key is public, anyone sending about two requests a
  second for it can keep its budget spent and make cold middleware instances refuse to let
  the widget be framed on its own site. A mismatch between the two services behaves exactly
  like unset.
- **`/api/v1/widget/<key>/config` is served from the web origin** (a rewrite in
  `next.config.ts`, `src/lib/auth-proxy.ts`'s `widgetConfigRewrites`), because the loader
  only knows the origin it was loaded from. The API answers it with
  `Access-Control-Allow-Origin: *` and `Cache-Control: public, max-age=60`; a proxy or CDN in
  front of the web app must pass both through unchanged, or the launcher silently draws
  nothing on customers' sites.

**uvicorn's own access log is off** (`--no-access-log`, already in `apps/api/Dockerfile`'s
`CMD` and `docker-compose.yml`'s `api` command — nothing to change for a Render deploy,
since `render.yaml` has no start command of its own and runs the Dockerfile's `CMD`
unmodified). Left on, it would print a second, unredacted access line per request — the
client IP and the full request path, a widget public key included — duplicating what
`app/main.py`'s own request-logging middleware already logs with the IP omitted and any
widget key truncated to 8 characters (spec §5: no IP in any log line).

## Seeding production

There is no automatic seed. To create the first organization and user, run
`uv run python -m app.db.seed` locally with `DATABASE_URL`/`MIGRATION_DATABASE_URL` pointed at Neon.

**The seed also switches on a public demo widget**, on whatever database it targets: it
activates the demo agent and enables its widget (daily cap 500, allowed origin
`http://localhost:5500`). That is what makes the local widget demo work; on a real database
it means a live, anonymous chat surface spending real model money up to that cap. If you seed
a real database, disable the demo agent's widget (Agents → the demo agent → Website widget)
or set the agent back to draft afterwards.

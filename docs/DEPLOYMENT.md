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
(`DATABASE_URL`, `MIGRATION_DATABASE_URL`, `REDIS_URL`, `CORS_ORIGINS`). `JWT_SECRET` is generated
by Render — copy it into the GitHub secret of the same name so the migrate job and the API agree.
Set `CORS_ORIGINS` to the Vercel production origin, e.g. `https://saas-ai.vercel.app`.

`autoDeploy` is off on purpose: deploys come from CI after the tests pass and the migration lands.
From the service's dashboard URL, take the `srv-…` id (`RENDER_SERVICE_ID`) and create an API key
under Account Settings → API Keys (`RENDER_API_KEY`).

Render's free instances spin down when idle, so the first request after a quiet period is slow.

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

## Seeding production

There is no automatic seed. To create the first organization and user, run
`uv run python -m app.db.seed` locally with `DATABASE_URL`/`MIGRATION_DATABASE_URL` pointed at Neon.

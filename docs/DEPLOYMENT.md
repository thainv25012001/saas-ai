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

The role split is what makes Row-Level Security real: `app_owner` owns the tables (and so bypasses
RLS) and only runs migrations; `app_user` is what the application connects as and is subject to
every policy. Neither role has `BYPASSRLS`.

Connection strings must be rewritten for SQLAlchemy + asyncpg — `asyncpg` does not understand
`sslmode` or `channel_binding`, so replace the query string Neon gives you with `?ssl=require`:

```
postgresql+asyncpg://app_user:<user-password>@<host>/neondb?ssl=require
postgresql+asyncpg://app_owner:<owner-password>@<host>/neondb?ssl=require
```

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

### 4. Vercel

Create the project and, because this workflow builds and uploads the app itself:

- Leave the project's **Root Directory** empty — the CLI runs from `apps/web`, which already *is*
  the app root. (Setting it to `apps/web` as well makes Vercel look for `apps/web/apps/web`.)
- Turn off the Git integration's automatic deploys (Settings → Git), or every push deploys twice,
  once unverified.
- Add `NEXT_PUBLIC_API_URL` to the Production environment, pointing at the Render URL. `vercel pull`
  brings it into the CI build.

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

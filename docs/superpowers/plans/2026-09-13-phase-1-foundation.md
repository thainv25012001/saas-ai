# Phase 1 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A running Next.js → GraphQL → FastAPI → PostgreSQL stack with authentication, organization/agent management, and database-enforced tenant isolation, startable from a clean clone with `docker compose up`.

**Architecture:** Modular monolith. FastAPI serves GraphQL (Strawberry) for the dashboard and REST for auth. PostgreSQL 16 with pgvector installed (unused until Phase 3) holds all data; every tenant-owned table carries `organization_id` and a Row-Level Security policy keyed on a per-transaction `SET LOCAL` setting. Next.js App Router consumes GraphQL through urql with generated types.

**Tech Stack:** Python 3.12, FastAPI, Strawberry GraphQL, SQLAlchemy 2.0 (async) + asyncpg, Alembic, pydantic-settings, structlog, argon2-cffi, PyJWT, redis-py; Next.js 15, TypeScript, Tailwind v4, urql, graphql-codegen; Docker Compose, uv, ruff, mypy, pytest.

**Spec:** [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) — Phase 1 is specified in §9; tenancy in §2.3; schema in §3.1–3.2; repo layout in §4.

## Global Constraints

- Python **3.12**; dependency management with **uv**; all API code lives under `apps/api/app/`.
- Node **22**; Next.js **15** App Router; TypeScript **strict**; Tailwind **v4**.
- PostgreSQL image **`pgvector/pgvector:pg16`**; Redis image **`redis:7-alpine`**.
- Every tenant-owned table has `organization_id UUID NOT NULL` + an RLS policy. The three exceptions are `organizations`, `users`, `memberships` (access to them is mediated by membership joins, not RLS).
- Primary keys are **UUIDv7**, generated in Python by `app.core.ids.uuid7()`. Never `uuid4`.
- The application connects to Postgres as role **`app_user`** (subject to RLS). Migrations connect as **`app_owner`** (table owner, bypasses RLS). Two separate URLs in the environment.
- No secret is ever exposed to the browser. `NEXT_PUBLIC_*` is only for the API base URL.
- `ruff check`, `ruff format --check`, and `mypy --strict app/` must pass on every commit.
- Every task ends with a commit. Commit messages use Conventional Commits (`feat:`, `test:`, `chore:`).
- **No LLM provider, no RAG, no tools, no embeddings in Phase 1.** pgvector is installed but no `vector` column is created.

## File Structure

```text
ai-sales-agent/
├── docker-compose.yml               # Task 1 (db, redis) → Task 13 (api, web)
├── Makefile                         # Task 1, extended by later tasks
├── .env.example                     # Task 1, extended by later tasks
├── infrastructure/postgres/init.sql # Task 1 — extensions + roles + default privileges
├── apps/api/
│   ├── pyproject.toml               # Task 2
│   ├── alembic.ini, alembic/        # Task 3
│   ├── app/
│   │   ├── main.py                  # Task 2 — app factory, router mounting
│   │   ├── core/
│   │   │   ├── config.py            # Task 2 — Settings
│   │   │   ├── ids.py               # Task 3 — uuid7()
│   │   │   ├── logging.py           # Task 2 — structlog + request_id
│   │   │   ├── errors.py            # Task 2 — AppError hierarchy
│   │   │   ├── security.py          # Task 5 — argon2 + JWT
│   │   │   ├── tenancy.py           # Task 4 — TenantContext + RLS session
│   │   │   ├── redis.py             # Task 6 — Redis client
│   │   │   └── rate_limit.py        # Task 6 — token bucket
│   │   ├── db/
│   │   │   ├── base.py              # Task 3 — Base + mixins
│   │   │   ├── session.py           # Task 3 — engine + session factory
│   │   │   ├── models/              # Tasks 3, 7, 8
│   │   │   └── repositories/        # Tasks 4, 7, 8
│   │   ├── auth/                    # Task 6 — service + router + schemas
│   │   ├── agents/                  # Task 7 — service + schemas
│   │   ├── prompts/                 # Task 8 — service + schemas
│   │   ├── graphql/                 # Task 9 — schema, context, types, resolvers
│   │   └── api/                     # Task 2 (health), Task 6 (auth router)
│   └── tests/                       # every task
└── apps/web/                        # Tasks 11, 12
```

Split rule: one module per domain concept, and files that change together live together. `db/models/` holds ORM classes only — no business logic; `<domain>/service.py` holds behaviour; `graphql/` only translates.

---

### Task 1: Infrastructure — Postgres with extensions and RLS roles

**Files:**
- Create: `docker-compose.yml`
- Create: `infrastructure/postgres/init.sql`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `Makefile`
- Test: `infrastructure/scripts/verify_db.sh`

**Interfaces:**
- Consumes: nothing.
- Produces: a Postgres instance on `localhost:5432` with extensions `vector`, `citext`, `pgcrypto`; roles `app_owner` (owner, LOGIN) and `app_user` (LOGIN, no BYPASSRLS); database `saas_ai`. A Redis instance on `localhost:6379`. Environment variable names `DATABASE_URL` (app_user) and `MIGRATION_DATABASE_URL` (app_owner).

- [ ] **Step 1: Write the verification script**

Create `infrastructure/scripts/verify_db.sh`:

```bash
#!/usr/bin/env bash
# Verifies the database container is provisioned correctly.
set -euo pipefail

psql_owner() { docker compose exec -T db psql -U app_owner -d saas_ai -tAc "$1"; }

echo "== extensions =="
for ext in vector citext pgcrypto; do
  found=$(psql_owner "SELECT 1 FROM pg_extension WHERE extname = '$ext'")
  [ "$found" = "1" ] || { echo "FAIL: extension $ext missing"; exit 1; }
  echo "ok: $ext"
done

echo "== roles =="
for role in app_owner app_user; do
  found=$(psql_owner "SELECT 1 FROM pg_roles WHERE rolname = '$role'")
  [ "$found" = "1" ] || { echo "FAIL: role $role missing"; exit 1; }
  echo "ok: $role"
done

echo "== app_user must NOT bypass RLS =="
bypass=$(psql_owner "SELECT rolbypassrls FROM pg_roles WHERE rolname = 'app_user'")
[ "$bypass" = "f" ] || { echo "FAIL: app_user has BYPASSRLS"; exit 1; }
echo "ok: app_user is subject to RLS"

echo "== redis =="
docker compose exec -T redis redis-cli ping | grep -q PONG || { echo "FAIL: redis"; exit 1; }
echo "ok: redis"

echo "ALL CHECKS PASSED"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash infrastructure/scripts/verify_db.sh`
Expected: FAIL — no compose file, so `docker compose exec` errors with "no configuration file provided".

- [ ] **Step 3: Write the Postgres init script**

Create `infrastructure/postgres/init.sql`. The official Postgres image runs files in `/docker-entrypoint-initdb.d` once, on an empty data directory, as the superuser against the `POSTGRES_DB`.

```sql
-- Runs once, as superuser, against the saas_ai database.

CREATE EXTENSION IF NOT EXISTS vector;    -- installed now, first used in Phase 3
CREATE EXTENSION IF NOT EXISTS citext;    -- case-insensitive email
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_bytes for API keys

-- app_owner owns every table and runs migrations. As table owner it bypasses
-- RLS, which is what lets migrations and seeds work unconditionally.
CREATE ROLE app_owner WITH LOGIN PASSWORD 'app_owner_password' NOBYPASSRLS;

-- app_user is what the application connects as. It is NOT the table owner,
-- so RLS policies apply to it. This separation is the whole point.
CREATE ROLE app_user WITH LOGIN PASSWORD 'app_user_password' NOBYPASSRLS;

GRANT CONNECT ON DATABASE saas_ai TO app_owner, app_user;
GRANT USAGE ON SCHEMA public TO app_owner, app_user;
GRANT CREATE ON SCHEMA public TO app_owner;

-- Anything app_owner creates later is automatically usable by app_user.
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO app_user;
```

> Passwords here are local-development only and are overridden by environment variables in any real deployment. They are committed on purpose so a clean clone works.

- [ ] **Step 4: Write docker-compose.yml**

Create `docker-compose.yml`:

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: saas_ai
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./infrastructure/postgres/init.sql:/docker-entrypoint-initdb.d/00-init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d saas_ai"]
      interval: 3s
      timeout: 3s
      retries: 20

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 3s
      timeout: 3s
      retries: 20

volumes:
  pgdata:
  redisdata:
```

- [ ] **Step 5: Write .env.example, .gitignore, and Makefile**

Create `.env.example`:

```bash
# ---- Database -------------------------------------------------------------
# The application connects as app_user and is subject to Row-Level Security.
DATABASE_URL=postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai
# Alembic connects as app_owner, which owns the tables and bypasses RLS.
MIGRATION_DATABASE_URL=postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai

# ---- Redis ----------------------------------------------------------------
REDIS_URL=redis://localhost:6379/0

# ---- Security -------------------------------------------------------------
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET=change-me-in-production
ACCESS_TOKEN_TTL_MINUTES=15
REFRESH_TOKEN_TTL_DAYS=30

# ---- App ------------------------------------------------------------------
ENVIRONMENT=local
LOG_LEVEL=INFO
CORS_ORIGINS=http://localhost:3000

# ---- Web ------------------------------------------------------------------
NEXT_PUBLIC_API_URL=http://localhost:8000
```

Create `.gitignore`:

```gitignore
.env
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
node_modules/
.next/
apps/web/src/graphql/generated.ts
```

Create `Makefile`:

```makefile
.PHONY: up down logs verify-db

up:
	docker compose up -d db redis

down:
	docker compose down

logs:
	docker compose logs -f

verify-db:
	bash infrastructure/scripts/verify_db.sh
```

- [ ] **Step 6: Bring it up and verify**

Run:
```bash
cp .env.example .env
make up
make verify-db
```
Expected: `ALL CHECKS PASSED`.

If extensions are missing, the volume was created before `init.sql` existed — run `docker compose down -v && make up` to recreate it. `init.sql` only runs on an **empty** data directory.

- [ ] **Step 7: Commit**

```bash
git add docker-compose.yml infrastructure .env.example .gitignore Makefile
git commit -m "chore: add postgres+pgvector and redis with RLS-ready roles"
```

---

### Task 2: FastAPI skeleton — config, logging, errors, health

**Files:**
- Create: `apps/api/pyproject.toml`
- Create: `apps/api/app/__init__.py`, `apps/api/app/core/__init__.py`, `apps/api/app/api/__init__.py`
- Create: `apps/api/app/core/config.py`
- Create: `apps/api/app/core/logging.py`
- Create: `apps/api/app/core/errors.py`
- Create: `apps/api/app/api/health.py`
- Create: `apps/api/app/main.py`
- Create: `apps/api/tests/conftest.py`
- Test: `apps/api/tests/unit/test_health.py`, `apps/api/tests/unit/test_errors.py`

**Interfaces:**
- Consumes: environment variables from Task 1.
- Produces:
  - `app.core.config.Settings` with fields `database_url: str`, `migration_database_url: str`, `redis_url: str`, `jwt_secret: str`, `access_token_ttl_minutes: int`, `refresh_token_ttl_days: int`, `environment: str`, `log_level: str`, `cors_origins: list[str]`; and `get_settings() -> Settings` (lru_cached).
  - `app.core.errors.AppError(message: str, *, code: str, status_code: int)` with subclasses `NotFoundError` (404, `not_found`), `ConflictError` (409, `conflict`), `AuthenticationError` (401, `unauthenticated`), `PermissionDeniedError` (403, `forbidden`), `ValidationError` (422, `invalid_input`), `RateLimitError` (429, `rate_limited`).
  - `app.core.logging.configure_logging(level: str) -> None`, `app.core.logging.request_id_var: ContextVar[str]`, `app.core.logging.get_logger(name: str)`.
  - `app.main.create_app() -> FastAPI`.
  - `GET /health` → `{"status": "ok"}`; `GET /health/ready` → `{"status": "ready", "checks": {...}}` (DB/Redis checks are added in Task 3 and Task 6; for now the checks dict is empty).
  - pytest fixture `client` → `httpx.AsyncClient` bound to the app.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_health.py`:

```python
import pytest


@pytest.mark.anyio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_health_response_carries_request_id_header(client):
    response = await client.get("/health")
    assert response.headers["x-request-id"]


@pytest.mark.anyio
async def test_supplied_request_id_is_echoed(client):
    response = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"
```

Create `apps/api/tests/unit/test_errors.py`:

```python
import pytest

from app.core.errors import AppError, ConflictError, NotFoundError


def test_not_found_error_carries_status_and_code():
    error = NotFoundError("agent not found")
    assert error.status_code == 404
    assert error.code == "not_found"
    assert str(error) == "agent not found"


def test_conflict_error_carries_status_and_code():
    error = ConflictError("slug already taken")
    assert error.status_code == 409
    assert error.code == "conflict"


def test_subclasses_are_app_errors():
    assert isinstance(NotFoundError("x"), AppError)


@pytest.mark.anyio
async def test_app_error_is_rendered_as_json_envelope(client):
    """The app registers a handler that turns AppError into a stable envelope."""
    response = await client.get("/health/boom")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "deliberate test failure"}
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/unit -v`
Expected: FAIL — `uv` cannot find `pyproject.toml`, or collection errors with `ModuleNotFoundError: No module named 'app'`.

- [ ] **Step 3: Write pyproject.toml**

Create `apps/api/pyproject.toml`:

```toml
[project]
name = "ai-sales-agent-api"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "structlog>=24.4",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "anyio>=4.6",
    "httpx>=0.27",
    "ruff>=0.7",
    "mypy>=1.13",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC", "T20"]

[tool.mypy]
python_version = "3.12"
strict = true
plugins = []

[[tool.mypy.overrides]]
module = "tests.*"
disallow_untyped_defs = false
```

- [ ] **Step 4: Write config, logging, and errors**

Create `apps/api/app/core/config.py`:

```python
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    migration_database_url: str
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    environment: str = "local"
    log_level: str = "INFO"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_comma_separated(cls, value: object) -> object:
        """CORS_ORIGINS is a comma-separated string in .env, a list in code."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
```

Create `apps/api/app/core/errors.py`:

```python
class AppError(Exception):
    """Base for every error the application raises deliberately.

    Carries the HTTP status and a stable machine-readable code so the REST
    and GraphQL layers can render it identically without re-deciding.
    """

    code = "internal_error"
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class ConflictError(AppError):
    code = "conflict"
    status_code = 409


class AuthenticationError(AppError):
    code = "unauthenticated"
    status_code = 401


class PermissionDeniedError(AppError):
    code = "forbidden"
    status_code = 403


class ValidationError(AppError):
    code = "invalid_input"
    status_code = 422


class RateLimitError(AppError):
    code = "rate_limited"
    status_code = 429
```

Create `apps/api/app/core/logging.py`:

```python
import logging
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def _add_request_id(
    _logger: Any, _method: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    request_id = request_id_var.get()
    if request_id:
        event_dict["request_id"] = request_id
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper()))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
```

- [ ] **Step 5: Write the health router and app factory**

Create `apps/api/app/api/health.py`:

```python
from typing import Any

from fastapi import APIRouter

from app.core.errors import NotFoundError

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health() -> dict[str, str]:
    """Liveness: the process is up. No dependencies are touched."""
    return {"status": "ok"}


@router.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness: dependencies are reachable.

    Task 3 adds the database check and Task 6 adds Redis.
    """
    return {"status": "ready", "checks": {}}


@router.get("/boom")
async def boom() -> dict[str, str]:
    """Exercises the AppError handler. Kept deliberately — it is the only
    end-to-end assertion that the error envelope is wired up."""
    raise NotFoundError("deliberate test failure")
```

Create `apps/api/app/main.py`:

```python
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import health
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, request_id_var

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="AI Sales Agent API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # required: the refresh token is a cookie
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        logger.warning("app_error", code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(health.router)
    return app


app = create_app()
```

Create empty `apps/api/app/__init__.py`, `apps/api/app/core/__init__.py`, `apps/api/app/api/__init__.py`, `apps/api/tests/__init__.py`, `apps/api/tests/unit/__init__.py`.

- [ ] **Step 6: Write the test harness**

Create `apps/api/tests/conftest.py`:

```python
import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

# Set before importing the app: Settings reads the environment at import time.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai",
)
os.environ.setdefault(
    "MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai",
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/unit -v`
Expected: 7 passed.

Then run: `uv run ruff check . && uv run mypy app/`
Expected: no errors.

- [ ] **Step 8: Extend the Makefile**

Append to `Makefile`:

```makefile
.PHONY: api test lint

api:
	cd apps/api && uv run uvicorn app.main:app --reload --port 8000

test:
	cd apps/api && uv run pytest -v

lint:
	cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app/
```

- [ ] **Step 9: Commit**

```bash
git add apps/api Makefile
git commit -m "feat: add FastAPI skeleton with structured logging and error envelope"
```

---

### Task 3: Database layer — async SQLAlchemy, UUIDv7, Alembic, identity tables with RLS

**Files:**
- Create: `apps/api/app/core/ids.py`
- Create: `apps/api/app/db/__init__.py`, `apps/api/app/db/base.py`, `apps/api/app/db/session.py`
- Create: `apps/api/app/db/models/__init__.py`, `apps/api/app/db/models/organization.py`, `apps/api/app/db/models/user.py`, `apps/api/app/db/models/membership.py`
- Create: `apps/api/alembic.ini`, `apps/api/alembic/env.py`, `apps/api/alembic/script.py.mako`
- Create: `apps/api/alembic/versions/0001_identity.py`
- Modify: `apps/api/pyproject.toml` (add sqlalchemy, asyncpg, alembic)
- Modify: `apps/api/app/api/health.py` (add the DB readiness check)
- Test: `apps/api/tests/unit/test_ids.py`, `apps/api/tests/integration/test_migrations.py`

**Interfaces:**
- Consumes: `Settings.database_url`, `Settings.migration_database_url` (Task 2).
- Produces:
  - `app.core.ids.uuid7() -> uuid.UUID` — time-ordered v7 identifier.
  - `app.db.base.Base` (DeclarativeBase), `app.db.base.TimestampMixin` (`created_at`, `updated_at`), `app.db.base.UUIDPrimaryKeyMixin` (`id`), `app.db.base.TenantMixin` (`organization_id`).
  - `app.db.session.engine`, `app.db.session.session_factory` (`async_sessionmaker[AsyncSession]`), `app.db.session.check_database() -> bool`.
  - ORM models `Organization`, `User`, `Membership`, and enum `MembershipRole` with values `OWNER`, `ADMIN`, `MEMBER`.
  - Alembic revision `0001_identity` creating `organizations`, `users`, `memberships`.
  - `app.db.base.enable_rls(op, table: str) -> None` — a migration helper used by Tasks 7 and 8.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_ids.py`:

```python
import time
from uuid import UUID

from app.core.ids import uuid7


def test_uuid7_reports_version_7():
    assert uuid7().version == 7


def test_uuid7_is_a_uuid():
    assert isinstance(uuid7(), UUID)


def test_uuid7_values_are_unique():
    values = {uuid7() for _ in range(10_000)}
    assert len(values) == 10_000


def test_uuid7_is_time_ordered():
    """The point of v7 over v4: sequential inserts stay index-local."""
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()
    assert first < second


def test_uuid7_encodes_current_timestamp():
    """The leading 48 bits are unix milliseconds."""
    before_ms = int(time.time() * 1000)
    value = uuid7()
    after_ms = int(time.time() * 1000)
    encoded_ms = value.int >> 80
    assert before_ms <= encoded_ms <= after_ms
```

Create `apps/api/tests/integration/test_migrations.py`:

```python
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.anyio


async def test_identity_tables_exist(owner_connection):
    result = await owner_connection.execute(
        text(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' ORDER BY tablename"
        )
    )
    tables = {row[0] for row in result}
    assert {"organizations", "users", "memberships"} <= tables


async def test_email_column_is_citext(owner_connection):
    """citext is what makes the unique index case-insensitive."""
    result = await owner_connection.execute(
        text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'email'"
        )
    )
    assert result.scalar_one() == "citext"


async def test_membership_is_unique_per_org_and_user(owner_connection):
    result = await owner_connection.execute(
        text(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE tablename = 'memberships' "
            "AND indexdef LIKE '%UNIQUE%organization_id, user_id%'"
        )
    )
    assert result.scalar_one() == 1


async def test_identity_tables_do_not_have_rls(owner_connection):
    """organizations/users/memberships are reached through membership joins,
    not through a tenant setting, so they are deliberately excluded from RLS."""
    result = await owner_connection.execute(
        text(
            "SELECT relrowsecurity FROM pg_class "
            "WHERE relname = 'users' AND relnamespace = 'public'::regnamespace"
        )
    )
    assert result.scalar_one() is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/unit/test_ids.py tests/integration -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.ids'` and `fixture 'owner_connection' not found`.

- [ ] **Step 3: Add dependencies**

Modify `apps/api/pyproject.toml`, adding to `dependencies`:

```toml
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "alembic>=1.14",
```

Run: `cd apps/api && uv sync`

- [ ] **Step 4: Implement uuid7**

Create `apps/api/app/core/ids.py`:

```python
import secrets
import time
import uuid

# RFC 9562 §5.7 layout:
#   48 bits unix_ts_ms | 4 bits version (7) | 12 bits rand_a
#   | 2 bits variant (0b10) | 62 bits rand_b
_VERSION = 0x7
_VARIANT = 0b10


def uuid7() -> uuid.UUID:
    """A time-ordered UUID.

    Used instead of uuid4 so that primary keys insert sequentially. Random
    keys scatter B-tree writes across the whole index; time-ordered keys keep
    them at the right edge, which matters once tables are large.
    """
    timestamp_ms = int(time.time() * 1000)
    value = (
        (timestamp_ms << 80)
        | (_VERSION << 76)
        | (secrets.randbits(12) << 64)
        | (_VARIANT << 62)
        | secrets.randbits(62)
    )
    return uuid.UUID(int=value)
```

- [ ] **Step 5: Run the id tests**

Run: `cd apps/api && uv run pytest tests/unit/test_ids.py -v`
Expected: 5 passed.

- [ ] **Step 6: Write the declarative base and mixins**

Create `apps/api/app/db/base.py`:

```python
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import uuid7


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid7
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Every tenant-owned table carries this. Pairing it with enable_rls() in
    the migration is what makes isolation structural rather than remembered."""

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )


def enable_rls(op: Any, table: str) -> None:
    """Turn on Row-Level Security for a tenant-owned table.

    The policy reads a per-transaction setting written by the tenant session
    dependency (see app/core/tenancy.py). The `true` second argument to
    current_setting makes a missing setting return NULL rather than raise,
    so an unset context yields zero rows instead of a 500.
    """
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (organization_id = current_setting('app.current_org_id', true)::uuid) "
        "WITH CHECK (organization_id = current_setting('app.current_org_id', true)::uuid)"
    )


def disable_rls(op: Any, table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
```

> `WITH CHECK` matters as much as `USING`: without it, a tenant could *insert* a row belonging to another organization even though it could not read one.

- [ ] **Step 7: Write the session module**

Create `apps/api/app/db/session.py`:

```python
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=10,
    max_overflow=5,
    pool_pre_ping=True,
    echo=False,
)

session_factory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def check_database() -> bool:
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - readiness must never raise
        return False
```

- [ ] **Step 8: Write the identity models**

Create `apps/api/app/db/models/organization.py`:

```python
from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
```

Create `apps/api/app/db/models/user.py`:

```python
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

Create `apps/api/app/db/models/membership.py`:

```python
import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class MembershipRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Membership(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_membership_org_user"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MembershipRole] = mapped_column(
        SAEnum(MembershipRole, name="membership_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=MembershipRole.MEMBER,
    )
```

Create `apps/api/app/db/models/__init__.py`:

```python
from app.db.models.membership import Membership, MembershipRole
from app.db.models.organization import Organization
from app.db.models.user import User

__all__ = ["Membership", "MembershipRole", "Organization", "User"]
```

- [ ] **Step 9: Configure Alembic**

Run: `cd apps/api && uv run alembic init -t async alembic`

Then replace `apps/api/alembic/env.py` with:

```python
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base
from app.db.models import *  # noqa: F401,F403 - registers models on Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Migrations run as app_owner, which owns the tables and so bypasses RLS.
config.set_main_option("sqlalchemy.url", get_settings().migration_database_url)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
```

In `apps/api/alembic.ini`, set `script_location = alembic` and remove any hard-coded `sqlalchemy.url` line (the URL comes from `env.py`).

- [ ] **Step 10: Write the initial migration**

Create `apps/api/alembic/versions/0001_identity.py`:

```python
"""identity: organizations, users, memberships

Revision ID: 0001_identity
Revises:
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("plan", sa.String(50), nullable=False, server_default="free"),
        sa.Column(
            "settings", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", "member", name="membership_role"),
            nullable=False,
            server_default="member",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "organization_id", "user_id", name="uq_membership_org_user"
        ),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")
    op.execute("DROP TYPE IF EXISTS membership_role")
    op.drop_table("users")
    op.drop_table("organizations")
```

- [ ] **Step 11: Add the database fixtures**

Append to `apps/api/tests/conftest.py`:

```python
@pytest.fixture
async def owner_connection():
    """A connection as app_owner — bypasses RLS. Use it to assert on schema
    and to set up fixture data across organizations."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url)
    async with engine.connect() as connection:
        yield connection
    await engine.dispose()
```

Create `apps/api/tests/integration/__init__.py` (empty).

- [ ] **Step 12: Run the migration and the tests**

Run:
```bash
cd apps/api && uv run alembic upgrade head
uv run pytest tests -v
```
Expected: migration applies cleanly; all tests pass (9 unit + 4 integration).

- [ ] **Step 13: Wire the database into readiness**

Modify `apps/api/app/api/health.py`, replacing the `ready` handler:

```python
@router.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness: dependencies are reachable. Redis is added in Task 6."""
    from app.db.session import check_database

    database_ok = await check_database()
    return {
        "status": "ready" if database_ok else "degraded",
        "checks": {"database": database_ok},
    }
```

Add to `apps/api/tests/integration/test_migrations.py`:

```python
async def test_readiness_reports_database_up(client):
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["database"] is True
```

- [ ] **Step 14: Extend the Makefile**

Append to `Makefile`:

```makefile
.PHONY: migrate revision

migrate:
	cd apps/api && uv run alembic upgrade head

revision:
	cd apps/api && uv run alembic revision -m "$(m)"
```

- [ ] **Step 15: Run everything and commit**

Run: `make migrate && make test && make lint`
Expected: all green.

```bash
git add apps/api Makefile
git commit -m "feat: add async SQLAlchemy, UUIDv7 keys, Alembic, and identity tables"
```

---

### Task 4: Tenancy — TenantContext, RLS-bound session, tenant repository base

**Files:**
- Create: `apps/api/app/core/tenancy.py`
- Create: `apps/api/app/db/repositories/__init__.py`, `apps/api/app/db/repositories/base.py`
- Create: `apps/api/alembic/versions/0002_rls_probe.py`
- Test: `apps/api/tests/integration/test_rls.py`

**Interfaces:**
- Consumes: `session_factory` (Task 3), `enable_rls` (Task 3), `PermissionDeniedError` (Task 2).
- Produces:
  - `app.core.tenancy.TenantContext` — a frozen dataclass with `organization_id: UUID`, `user_id: UUID | None`, `role: MembershipRole | None`, `request_id: str`.
  - `app.core.tenancy.tenant_session(tenant: TenantContext) -> AsyncIterator[AsyncSession]` — async context manager that opens a transaction, sets `app.current_org_id`, yields the session, and commits on clean exit.
  - `app.core.tenancy.untenanted_session() -> AsyncIterator[AsyncSession]` — for registration and login, which run before any organization is known.
  - `app.db.repositories.base.TenantRepository` — generic base with `__init__(self, session: AsyncSession, tenant: TenantContext)` and helpers `select_scoped(stmt)`, `get_or_raise(model, id)`.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/integration/test_rls.py`. This is the most important test file in Phase 1 — it proves isolation is real rather than intended.

```python
import uuid

import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.tenancy import TenantContext, tenant_session

pytestmark = pytest.mark.anyio


@pytest.fixture
async def two_orgs(owner_connection):
    """Two organizations, each with one row in the RLS probe table."""
    org_a, org_b = uuid7(), uuid7()
    for org_id, name in ((org_a, "Org A"), (org_b, "Org B")):
        await owner_connection.execute(
            text(
                "INSERT INTO organizations (id, name, slug, plan, settings) "
                "VALUES (:id, :name, :slug, 'free', '{}')"
            ),
            {"id": org_id, "name": name, "slug": name.lower().replace(" ", "-")},
        )
        await owner_connection.execute(
            text(
                "INSERT INTO rls_probe (id, organization_id, label) "
                "VALUES (:id, :org, :label)"
            ),
            {"id": uuid7(), "org": org_id, "label": f"{name} secret"},
        )
    await owner_connection.commit()
    yield org_a, org_b
    await owner_connection.execute(
        text("DELETE FROM organizations WHERE id IN (:a, :b)"),
        {"a": org_a, "b": org_b},
    )
    await owner_connection.commit()


def _context(org_id: uuid.UUID) -> TenantContext:
    return TenantContext(
        organization_id=org_id, user_id=None, role=None, request_id="test"
    )


async def test_tenant_sees_only_its_own_rows(two_orgs):
    org_a, _org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        result = await session.execute(text("SELECT label FROM rls_probe"))
        labels = [row[0] for row in result]
    assert labels == ["Org A secret"]


async def test_tenant_cannot_see_the_other_tenants_rows(two_orgs):
    org_a, org_b = two_orgs
    async with tenant_session(_context(org_b)) as session:
        result = await session.execute(text("SELECT label FROM rls_probe"))
        labels = [row[0] for row in result]
    assert "Org A secret" not in labels


async def test_targeting_another_tenants_row_by_id_returns_nothing(two_orgs):
    """Even with the exact primary key, the row is invisible."""
    org_a, org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        row_id = (
            await session.execute(text("SELECT id FROM rls_probe"))
        ).scalar_one()

    async with tenant_session(_context(org_b)) as session:
        result = await session.execute(
            text("SELECT label FROM rls_probe WHERE id = :id"), {"id": row_id}
        )
        assert result.first() is None


async def test_insert_for_another_tenant_is_rejected(two_orgs):
    """WITH CHECK: a tenant cannot write a row it would not be able to read."""
    from sqlalchemy.exc import DBAPIError

    org_a, org_b = two_orgs
    with pytest.raises(DBAPIError):
        async with tenant_session(_context(org_a)) as session:
            await session.execute(
                text(
                    "INSERT INTO rls_probe (id, organization_id, label) "
                    "VALUES (:id, :org, 'smuggled')"
                ),
                {"id": uuid7(), "org": org_b},
            )


async def test_update_cannot_move_a_row_to_another_tenant(two_orgs):
    from sqlalchemy.exc import DBAPIError

    org_a, org_b = two_orgs
    with pytest.raises(DBAPIError):
        async with tenant_session(_context(org_a)) as session:
            await session.execute(
                text("UPDATE rls_probe SET organization_id = :org"), {"org": org_b}
            )


async def test_setting_does_not_leak_between_sessions(two_orgs):
    """SET LOCAL dies with the transaction. If it leaked through the pool,
    a later request could inherit a previous tenant's context."""
    org_a, org_b = two_orgs
    async with tenant_session(_context(org_a)) as session:
        await session.execute(text("SELECT 1"))

    async with tenant_session(_context(org_b)) as session:
        current = (
            await session.execute(
                text("SELECT current_setting('app.current_org_id', true)")
            )
        ).scalar_one()
    assert current == str(org_b)


async def test_missing_tenant_setting_yields_no_rows(two_orgs):
    """An unset context must be an empty result, never an error and never
    every row."""
    from app.db.session import session_factory

    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(text("SELECT label FROM rls_probe"))
            assert result.all() == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/integration/test_rls.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.tenancy'`.

- [ ] **Step 3: Write the RLS probe migration**

Create `apps/api/alembic/versions/0002_rls_probe.py`. This table exists only so RLS behaviour can be tested independently of any business table — it is the fixture that makes the isolation suite meaningful before `agents` exists.

```python
"""rls_probe: a minimal tenant-owned table used to test RLS itself

Revision ID: 0002_rls_probe
Revises: 0001_identity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.base import disable_rls, enable_rls

revision = "0002_rls_probe"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rls_probe",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(255), nullable=False),
    )
    op.create_index("ix_rls_probe_org", "rls_probe", ["organization_id"])
    enable_rls(op, "rls_probe")


def downgrade() -> None:
    disable_rls(op, "rls_probe")
    op.drop_index("ix_rls_probe_org", table_name="rls_probe")
    op.drop_table("rls_probe")
```

- [ ] **Step 4: Implement tenancy**

Create `apps/api/app/core/tenancy.py`:

```python
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MembershipRole
from app.db.session import session_factory


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Who is acting, and on behalf of which organization.

    Always derived on the server from an authenticated token or from the
    conversation's agent. Never from client-supplied or model-supplied input.
    """

    organization_id: uuid.UUID
    user_id: uuid.UUID | None
    role: MembershipRole | None
    request_id: str


@asynccontextmanager
async def tenant_session(tenant: TenantContext) -> AsyncIterator[AsyncSession]:
    """Open a transaction with the tenant setting applied.

    set_config(..., is_local=true) is transaction-scoped, so the value is
    discarded when the transaction ends and cannot survive on a pooled
    connection into somebody else's request. Everything inside the block runs
    in one transaction: committing mid-block would drop the setting.
    """
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.current_org_id', :org_id, true)"),
                {"org_id": str(tenant.organization_id)},
            )
            yield session


@asynccontextmanager
async def untenanted_session() -> AsyncIterator[AsyncSession]:
    """For work that happens before an organization is known: registration,
    login, and refresh. Only reaches organizations/users/memberships, which
    are deliberately not under RLS."""
    async with session_factory() as session:
        async with session.begin():
            yield session
```

- [ ] **Step 5: Write the tenant repository base**

Create `apps/api/app/db/repositories/base.py`:

```python
import uuid
from typing import Any, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.tenancy import TenantContext
from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class TenantRepository:
    """Base for repositories over tenant-owned tables.

    There is no constructor that omits the tenant. RLS is the backstop; this
    is the layer that means the backstop is never reached in normal operation.
    """

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    @property
    def organization_id(self) -> uuid.UUID:
        return self.tenant.organization_id

    def select_scoped(self, model: type[ModelT]) -> Select[tuple[ModelT]]:
        return select(model).where(
            model.organization_id == self.organization_id  # type: ignore[attr-defined]
        )

    async def get_or_raise(
        self, model: type[ModelT], entity_id: uuid.UUID, *, label: str
    ) -> ModelT:
        stmt = self.select_scoped(model).where(model.id == entity_id)  # type: ignore[attr-defined]
        result = await self.session.execute(stmt)
        entity: Any = result.scalar_one_or_none()
        if entity is None:
            raise NotFoundError(f"{label} not found")
        return entity  # type: ignore[no-any-return]
```

Create empty `apps/api/app/db/repositories/__init__.py`.

- [ ] **Step 6: Migrate and run the tests**

Run:
```bash
cd apps/api && uv run alembic upgrade head
uv run pytest tests/integration/test_rls.py -v
```
Expected: 7 passed.

If `test_insert_for_another_tenant_is_rejected` fails with no exception raised, the policy is missing its `WITH CHECK` clause. If every test returns all rows, the app is connecting as `app_owner` — check `DATABASE_URL` uses `app_user`.

- [ ] **Step 7: Commit**

```bash
git add apps/api
git commit -m "feat: add tenant context with transaction-scoped RLS binding"
```

---

### Task 5: Security primitives — password hashing and JWTs

**Files:**
- Create: `apps/api/app/core/security.py`
- Modify: `apps/api/pyproject.toml` (add argon2-cffi, pyjwt)
- Test: `apps/api/tests/unit/test_security.py`

**Interfaces:**
- Consumes: `Settings.jwt_secret`, `Settings.access_token_ttl_minutes`, `Settings.refresh_token_ttl_days` (Task 2).
- Produces:
  - `hash_password(password: str) -> str`
  - `verify_password(password: str, password_hash: str) -> bool`
  - `create_access_token(*, user_id: UUID, organization_id: UUID, role: str) -> str`
  - `create_refresh_token(*, user_id: UUID) -> tuple[str, str]` — returns `(token, jti)`
  - `decode_token(token: str, *, expected_type: str) -> TokenPayload`
  - `TokenPayload` — pydantic model with `sub: UUID`, `org: UUID | None`, `role: str | None`, `jti: str`, `typ: str`, `exp: int`.
  - Raises `AuthenticationError` on any invalid, expired, or wrong-type token.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/unit/test_security.py`:

```python
import time
from datetime import timedelta

import pytest

from app.core.errors import AuthenticationError
from app.core.ids import uuid7
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_is_not_the_plaintext():
    assert hash_password("hunter2") != "hunter2"


def test_hash_uses_argon2id():
    assert hash_password("hunter2").startswith("$argon2id$")


def test_hashes_are_salted_and_therefore_differ():
    assert hash_password("hunter2") != hash_password("hunter2")


def test_correct_password_verifies():
    assert verify_password("hunter2", hash_password("hunter2")) is True


def test_wrong_password_does_not_verify():
    assert verify_password("wrong", hash_password("hunter2")) is False


def test_malformed_hash_does_not_verify_and_does_not_raise():
    assert verify_password("hunter2", "not-a-hash") is False


def test_access_token_round_trips_its_claims():
    user_id, org_id = uuid7(), uuid7()
    token = create_access_token(user_id=user_id, organization_id=org_id, role="owner")
    payload = decode_token(token, expected_type="access")
    assert payload.sub == user_id
    assert payload.org == org_id
    assert payload.role == "owner"
    assert payload.typ == "access"


def test_refresh_token_returns_a_jti_matching_its_payload():
    token, jti = create_refresh_token(user_id=uuid7())
    payload = decode_token(token, expected_type="refresh")
    assert payload.jti == jti


def test_a_refresh_token_is_rejected_where_an_access_token_is_required():
    """Token confusion is a real attack: a long-lived refresh token must not
    be usable as a short-lived access token."""
    token, _ = create_refresh_token(user_id=uuid7())
    with pytest.raises(AuthenticationError):
        decode_token(token, expected_type="access")


def test_tampered_token_is_rejected():
    token = create_access_token(
        user_id=uuid7(), organization_id=uuid7(), role="owner"
    )
    with pytest.raises(AuthenticationError):
        decode_token(token + "x", expected_type="access")


def test_garbage_is_rejected():
    with pytest.raises(AuthenticationError):
        decode_token("not.a.token", expected_type="access")


def test_expired_token_is_rejected(monkeypatch):
    from app.core import security

    monkeypatch.setattr(
        security, "_access_token_lifetime", lambda: timedelta(seconds=-1)
    )
    token = security.create_access_token(
        user_id=uuid7(), organization_id=uuid7(), role="owner"
    )
    time.sleep(0.01)
    with pytest.raises(AuthenticationError):
        security.decode_token(token, expected_type="access")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/unit/test_security.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.security'`.

- [ ] **Step 3: Add dependencies**

Modify `apps/api/pyproject.toml`, adding to `dependencies`:

```toml
    "argon2-cffi>=23.1",
    "pyjwt>=2.9",
```

Run: `cd apps/api && uv sync`

- [ ] **Step 4: Implement security**

Create `apps/api/app/core/security.py`:

```python
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import AuthenticationError

_ALGORITHM = "HS256"
_hasher = PasswordHasher()


class TokenPayload(BaseModel):
    sub: uuid.UUID
    org: uuid.UUID | None = None
    role: str | None = None
    jti: str
    typ: str
    exp: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """False for a wrong password and for a corrupt hash alike. Callers get a
    boolean, never an exception they might forget to catch."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, Exception):  # noqa: B014
        return False


def _access_token_lifetime() -> timedelta:
    return timedelta(minutes=get_settings().access_token_ttl_minutes)


def _refresh_token_lifetime() -> timedelta:
    return timedelta(days=get_settings().refresh_token_ttl_days)


def _encode(claims: dict[str, object], lifetime: timedelta) -> str:
    payload = {
        **claims,
        "exp": int((datetime.now(UTC) + lifetime).timestamp()),
        "iat": int(datetime.now(UTC).timestamp()),
    }
    return jwt.encode(payload, get_settings().jwt_secret, algorithm=_ALGORITHM)


def create_access_token(
    *, user_id: uuid.UUID, organization_id: uuid.UUID, role: str
) -> str:
    return _encode(
        {
            "sub": str(user_id),
            "org": str(organization_id),
            "role": role,
            "jti": str(uuid.uuid4()),
            "typ": "access",
        },
        _access_token_lifetime(),
    )


def create_refresh_token(*, user_id: uuid.UUID) -> tuple[str, str]:
    """Returns (token, jti). The jti is what logout adds to the Redis denylist."""
    jti = str(uuid.uuid4())
    token = _encode(
        {"sub": str(user_id), "jti": jti, "typ": "refresh"},
        _refresh_token_lifetime(),
    )
    return token, jti


def decode_token(token: str, *, expected_type: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, get_settings().jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise AuthenticationError("invalid or expired token") from exc

    payload = TokenPayload.model_validate(raw)
    if payload.typ != expected_type:
        raise AuthenticationError("invalid or expired token")
    return payload


def refresh_token_ttl_seconds() -> int:
    return int(_refresh_token_lifetime().total_seconds())
```

> The error message is identical for expired, tampered, and wrong-type tokens on purpose: the client has nothing useful to do with the distinction, and a precise message tells an attacker which half of their guess was right.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/unit/test_security.py -v`
Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/api
git commit -m "feat: add argon2 password hashing and typed JWT issuance"
```

---

### Task 6: Authentication — service, REST endpoints, rate limiting

**Files:**
- Create: `apps/api/app/core/redis.py`, `apps/api/app/core/rate_limit.py`
- Create: `apps/api/app/auth/__init__.py`, `apps/api/app/auth/schemas.py`, `apps/api/app/auth/service.py`, `apps/api/app/auth/dependencies.py`
- Create: `apps/api/app/api/auth.py`
- Modify: `apps/api/app/main.py` (mount the auth router)
- Modify: `apps/api/app/api/health.py` (add the Redis readiness check)
- Modify: `apps/api/pyproject.toml` (add redis, python-slugify)
- Test: `apps/api/tests/integration/test_auth.py`

**Interfaces:**
- Consumes: `hash_password`, `verify_password`, `create_access_token`, `create_refresh_token`, `decode_token`, `refresh_token_ttl_seconds` (Task 5); `untenanted_session`, `TenantContext` (Task 4); `Organization`, `User`, `Membership`, `MembershipRole` (Task 3).
- Produces:
  - `app.auth.schemas.RegisterRequest` (`email: EmailStr`, `password: str` min 12, `full_name: str`, `organization_name: str`), `LoginRequest` (`email`, `password`), `TokenResponse` (`access_token: str`, `expires_in: int`), `MeResponse` (`user_id`, `email`, `full_name`, `organization_id`, `organization_name`, `role`).
  - `app.auth.service.AuthService` with `register(request) -> tuple[User, Organization, Membership]`, `authenticate(email, password) -> tuple[User, Membership]`, `issue_tokens(user, membership) -> tuple[str, str, str]` returning `(access, refresh, jti)`, `rotate_refresh(refresh_token) -> tuple[str, str, str]`, `revoke_refresh(jti, ttl) -> None`.
  - `app.auth.dependencies.get_current_tenant(request) -> TenantContext` — a FastAPI dependency that reads `Authorization: Bearer` and raises `AuthenticationError` when absent or invalid. **Task 9's GraphQL context depends on this.**
  - REST: `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`.
  - The refresh token is delivered as an httpOnly, SameSite=Lax cookie named `refresh_token`; the access token is returned in the JSON body and never stored by the browser beyond memory.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/integration/test_auth.py`:

```python
import pytest

pytestmark = pytest.mark.anyio

REGISTRATION = {
    "email": "owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Ada Owner",
    "organization_name": "Ada Motors",
}


async def test_register_returns_an_access_token(client, clean_users):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 201
    body = response.json()
    assert body["access_token"]
    assert body["expires_in"] > 0


async def test_register_sets_an_httponly_refresh_cookie(client, clean_users):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("SameSite=Lax", "SameSite=lax")


async def test_register_creates_org_user_and_owner_membership(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTRATION["email"], "password": REGISTRATION["password"]},
    )
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    body = me.json()
    assert body["email"] == REGISTRATION["email"]
    assert body["organization_name"] == "Ada Motors"
    assert body["role"] == "owner"


async def test_duplicate_email_is_a_conflict(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_email_uniqueness_is_case_insensitive(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "email": "OWNER@example.com"}
    )
    assert response.status_code == 409


async def test_short_password_is_rejected(client, clean_users):
    response = await client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "password": "short"}
    )
    assert response.status_code == 422


async def test_login_with_wrong_password_is_rejected(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTRATION["email"], "password": "wrong-password-entirely"},
    )
    assert response.status_code == 401


async def test_login_for_unknown_email_gives_the_same_error_as_wrong_password(
    client, clean_users
):
    """Identical responses: a different one enumerates registered emails."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_me_requires_a_token(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_me_rejects_a_garbage_token(client):
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"}
    )
    assert response.status_code == 401


async def test_refresh_issues_a_new_access_token(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_refresh_without_a_cookie_is_rejected(client):
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


async def test_logout_revokes_the_refresh_token(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_repeated_failed_logins_are_rate_limited(client, clean_users):
    """Ten attempts per minute per IP. Attempt eleven is refused."""
    for _ in range(10):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever-long-enough"},
        )
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever-long-enough"},
    )
    assert response.status_code == 429
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/integration/test_auth.py -v`
Expected: FAIL — `fixture 'clean_users' not found`, and 404s on every auth route.

- [ ] **Step 3: Add dependencies and the cleanup fixture**

Modify `apps/api/pyproject.toml`, adding to `dependencies`:

```toml
    "redis>=5.2",
    "python-slugify>=8.0",
    "email-validator>=2.2",
```

Run: `cd apps/api && uv sync`

Append to `apps/api/tests/conftest.py`:

```python
@pytest.fixture
async def clean_users(owner_connection):
    """Auth tests share a database. Remove test rows before and after so each
    test starts from a known state, and rate-limit counters do not bleed."""
    from sqlalchemy import text

    async def _purge():
        await owner_connection.execute(
            text("DELETE FROM users WHERE email LIKE '%@example.com'")
        )
        await owner_connection.execute(
            text("DELETE FROM organizations WHERE slug LIKE 'ada-motors%'")
        )
        await owner_connection.commit()

    await _purge()
    await _flush_rate_limits()
    yield
    await _purge()


async def _flush_rate_limits() -> None:
    from app.core.redis import get_redis

    redis = get_redis()
    keys = [key async for key in redis.scan_iter("ratelimit:*")]
    if keys:
        await redis.delete(*keys)
```

- [ ] **Step 4: Implement Redis and rate limiting**

Create `apps/api/app/core/redis.py`:

```python
from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:  # noqa: BLE001 - readiness must never raise
        return False
```

Create `apps/api/app/core/rate_limit.py`:

```python
from app.core.errors import RateLimitError
from app.core.redis import get_redis


async def enforce_rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    """Fixed-window counter in Redis.

    Chosen over a sliding window because it is two commands and the failure
    mode (up to 2x the limit across a window boundary) is irrelevant for
    login throttling.
    """
    redis = get_redis()
    redis_key = f"ratelimit:{key}"
    count = await redis.incr(redis_key)
    if count == 1:
        await redis.expire(redis_key, window_seconds)
    if count > limit:
        raise RateLimitError("too many requests, please try again shortly")
```

- [ ] **Step 5: Implement the auth schemas**

Create `apps/api/app/auth/schemas.py`:

```python
import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)
    organization_name: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    expires_in: int


class MeResponse(BaseModel):
    user_id: uuid.UUID
    email: str
    full_name: str
    organization_id: uuid.UUID
    organization_name: str
    role: str
```

- [ ] **Step 6: Implement the auth service**

Create `apps/api/app/auth/service.py`:

```python
import uuid
from datetime import UTC, datetime

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import RegisterRequest
from app.core.errors import AuthenticationError, ConflictError
from app.core.ids import uuid7
from app.core.redis import get_redis
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    refresh_token_ttl_seconds,
    verify_password,
)
from app.db.models import Membership, MembershipRole, Organization, User

_DENYLIST_PREFIX = "refresh:revoked:"


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(
        self, request: RegisterRequest
    ) -> tuple[User, Organization, Membership]:
        """Creates organization, user, and owner membership in one transaction.
        A partial result here would leave an account that cannot be used."""
        existing = await self.session.execute(
            select(User.id).where(User.email == request.email)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("an account with that email already exists")

        organization = Organization(
            id=uuid7(),
            name=request.organization_name,
            slug=await self._unique_slug(request.organization_name),
        )
        user = User(
            id=uuid7(),
            email=request.email,
            password_hash=hash_password(request.password),
            full_name=request.full_name,
        )
        membership = Membership(
            id=uuid7(),
            organization_id=organization.id,
            user_id=user.id,
            role=MembershipRole.OWNER,
        )
        self.session.add_all([organization, user, membership])
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # Two concurrent registrations for the same email: the unique
            # index is the authority, not the SELECT above.
            raise ConflictError("an account with that email already exists") from exc
        return user, organization, membership

    async def _unique_slug(self, name: str) -> str:
        base = slugify(name)[:90] or "org"
        candidate = base
        for suffix in range(1, 100):
            taken = await self.session.execute(
                select(Organization.id).where(Organization.slug == candidate)
            )
            if taken.scalar_one_or_none() is None:
                return candidate
            candidate = f"{base}-{suffix}"
        return f"{base}-{uuid.uuid4().hex[:8]}"

    async def authenticate(self, email: str, password: str) -> tuple[User, Membership]:
        result = await self.session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        # Hash even when the user is absent, so response time does not reveal
        # whether an email is registered.
        password_hash = user.password_hash if user else hash_password("dummy")
        if not verify_password(password, password_hash) or user is None:
            raise AuthenticationError("invalid email or password")
        if not user.is_active:
            raise AuthenticationError("invalid email or password")

        membership = await self._primary_membership(user.id)
        user.last_login_at = datetime.now(UTC)
        return user, membership

    async def _primary_membership(self, user_id: uuid.UUID) -> Membership:
        """Phase 1: a user belongs to exactly one organization, so the oldest
        membership is the active one. Org switching arrives with Phase 7."""
        result = await self.session.execute(
            select(Membership)
            .where(Membership.user_id == user_id)
            .order_by(Membership.created_at)
            .limit(1)
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise AuthenticationError("invalid email or password")
        return membership

    def issue_tokens(self, user: User, membership: Membership) -> tuple[str, str, str]:
        access = create_access_token(
            user_id=user.id,
            organization_id=membership.organization_id,
            role=membership.role.value,
        )
        refresh, jti = create_refresh_token(user_id=user.id)
        return access, refresh, jti

    async def rotate_refresh(self, refresh_token: str) -> tuple[str, str, str]:
        payload = decode_token(refresh_token, expected_type="refresh")
        if await get_redis().exists(f"{_DENYLIST_PREFIX}{payload.jti}"):
            raise AuthenticationError("invalid or expired token")

        result = await self.session.execute(select(User).where(User.id == payload.sub))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise AuthenticationError("invalid or expired token")

        # Rotation: the presented token is burned as the new pair is issued,
        # so a stolen refresh token is usable at most once.
        await self.revoke_refresh(payload.jti)
        membership = await self._primary_membership(user.id)
        return self.issue_tokens(user, membership)

    async def revoke_refresh(self, jti: str) -> None:
        await get_redis().setex(
            f"{_DENYLIST_PREFIX}{jti}", refresh_token_ttl_seconds(), "1"
        )
```

- [ ] **Step 7: Implement the current-tenant dependency**

Create `apps/api/app/auth/dependencies.py`:

```python
from fastapi import Request

from app.core.errors import AuthenticationError
from app.core.logging import request_id_var
from app.core.security import decode_token
from app.core.tenancy import TenantContext
from app.db.models import MembershipRole


def tenant_from_bearer(request: Request) -> TenantContext:
    """Build the tenant context from the Authorization header.

    Used by the REST routes and, from Task 9, by the GraphQL context.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("missing bearer token")

    payload = decode_token(token, expected_type="access")
    if payload.org is None or payload.role is None:
        raise AuthenticationError("token is missing organization context")

    return TenantContext(
        organization_id=payload.org,
        user_id=payload.sub,
        role=MembershipRole(payload.role),
        request_id=request_id_var.get(),
    )


async def get_current_tenant(request: Request) -> TenantContext:
    return tenant_from_bearer(request)
```

- [ ] **Step 8: Implement the auth router**

Create `apps/api/app/api/auth.py`:

```python
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.auth.dependencies import get_current_tenant
from app.auth.schemas import LoginRequest, MeResponse, RegisterRequest, TokenResponse
from app.auth.service import AuthService
from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.rate_limit import enforce_rate_limit
from app.core.security import decode_token, refresh_token_ttl_seconds
from app.core.tenancy import TenantContext, untenanted_session
from app.db.models import Membership, Organization, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=refresh_token_ttl_seconds(),
        httponly=True,          # unreachable from JavaScript, so XSS cannot steal it
        secure=get_settings().environment != "local",
        samesite="lax",
        path="/api/v1/auth",    # sent only to the endpoints that need it
    )


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest, request: Request, response: Response
) -> TokenResponse:
    await enforce_rate_limit(
        f"register:{_client_key(request)}", limit=5, window_seconds=3600
    )
    async with untenanted_session() as session:
        service = AuthService(session)
        user, _organization, membership = await service.register(payload)
        access, refresh, _jti = service.issue_tokens(user, membership)

    _set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.post("/login")
async def login(
    payload: LoginRequest, request: Request, response: Response
) -> TokenResponse:
    await enforce_rate_limit(
        f"login:{_client_key(request)}", limit=10, window_seconds=60
    )
    async with untenanted_session() as session:
        service = AuthService(session)
        user, membership = await service.authenticate(payload.email, payload.password)
        access, refresh, _jti = service.issue_tokens(user, membership)

    _set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.post("/refresh")
async def refresh_tokens(request: Request, response: Response) -> TokenResponse:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthenticationError("missing refresh token")

    async with untenanted_session() as session:
        access, refresh, _jti = await AuthService(session).rotate_refresh(token)

    _set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, expected_type="refresh")
        except AuthenticationError:
            payload = None  # already invalid; clearing the cookie is enough
        if payload is not None:
            async with untenanted_session() as session:
                await AuthService(session).revoke_refresh(payload.jti)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")


@router.get("/me")
async def me(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> MeResponse:
    async with untenanted_session() as session:
        result = await session.execute(
            select(User, Organization, Membership)
            .join(Membership, Membership.user_id == User.id)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(
                User.id == tenant.user_id,
                Membership.organization_id == tenant.organization_id,
            )
        )
        row = result.first()
        if row is None:
            raise AuthenticationError("account no longer exists")
        user, organization, membership = row

        return MeResponse(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            organization_id=organization.id,
            organization_name=organization.name,
            role=membership.role.value,
        )
```

- [ ] **Step 9: Mount the router and finish readiness**

Modify `apps/api/app/main.py`: add `from app.api import auth, health` and, next to the existing include, `app.include_router(auth.router)`.

Modify `apps/api/app/api/health.py`, replacing the `ready` handler:

```python
@router.get("/ready")
async def ready() -> dict[str, Any]:
    from app.core.redis import check_redis
    from app.db.session import check_database

    database_ok = await check_database()
    redis_ok = await check_redis()
    return {
        "status": "ready" if database_ok and redis_ok else "degraded",
        "checks": {"database": database_ok, "redis": redis_ok},
    }
```

Update the readiness assertion in `apps/api/tests/integration/test_migrations.py`:

```python
async def test_readiness_reports_dependencies_up(client):
    response = await client.get("/health/ready")
    assert response.json()["checks"] == {"database": True, "redis": True}
```

(Replace the earlier `test_readiness_reports_database_up` with this.)

- [ ] **Step 10: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests -v`
Expected: all pass, including 14 auth tests.

If `test_repeated_failed_logins_are_rate_limited` fails intermittently, the Redis counter survived from a previous run — confirm `_flush_rate_limits` is called by the `clean_users` fixture.

- [ ] **Step 11: Commit**

```bash
git add apps/api
git commit -m "feat: add registration, login, refresh rotation, and rate limiting"
```

---

### Task 7: Agents — model, config, migration, repository, service

**Files:**
- Create: `apps/api/app/db/models/agent.py`
- Create: `apps/api/app/db/repositories/agent.py`
- Create: `apps/api/app/agents/__init__.py`, `apps/api/app/agents/schemas.py`, `apps/api/app/agents/service.py`
- Modify: `apps/api/app/db/models/__init__.py`
- Create: `apps/api/alembic/versions/0003_agents.py`
- Test: `apps/api/tests/integration/test_agent_service.py`

**Interfaces:**
- Consumes: `TenantRepository` (Task 4), `tenant_session`, `TenantContext` (Task 4), `Base`/mixins/`enable_rls` (Task 3), `NotFoundError`/`ConflictError` (Task 2).
- Produces:
  - `Agent` model: `id`, `organization_id`, `name`, `slug`, `status: AgentStatus`, `provider`, `model`, `temperature`, `max_tokens`, `prompt_id`, `public_key`, timestamps. Unique on `(organization_id, slug)`.
  - `AgentStatus` enum: `DRAFT`, `ACTIVE`, `DISABLED`.
  - `AgentConfig` model, 1:1 on `agent_id`: `persona`, `tone`, `language`, `greeting`, `fallback_message`, `enabled_tool_names: list[str]`, `retrieval_top_k`, `retrieval_min_score`, `max_agent_steps`, `guardrails: dict`, `variables: dict`.
  - `app.agents.schemas.CreateAgentInput` (`name`, `provider`, `model`, `temperature`, `max_tokens`), `UpdateAgentInput` (all optional), `UpdateAgentConfigInput` (all optional).
  - `app.agents.service.AgentService(session, tenant)` with `list_agents() -> list[Agent]`, `get_agent(id) -> Agent`, `create_agent(input) -> Agent`, `update_agent(id, input) -> Agent`, `update_config(agent_id, input) -> AgentConfig`, `delete_agent(id) -> None`.
  - **Task 9 calls exactly these method names.**

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/integration/test_agent_service.py`:

```python
import pytest

from app.agents.schemas import CreateAgentInput, UpdateAgentConfigInput, UpdateAgentInput
from app.agents.service import AgentService
from app.core.errors import ConflictError, NotFoundError
from app.core.tenancy import tenant_session

pytestmark = pytest.mark.anyio


async def test_create_agent_assigns_a_slug(tenant_a):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    assert agent.slug == "sales-bot"


async def test_create_agent_starts_in_draft(tenant_a):
    from app.db.models import AgentStatus

    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    assert agent.status is AgentStatus.DRAFT


async def test_create_agent_creates_a_default_config(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot"))
        config = await service.get_config(agent.id)
    assert config.retrieval_top_k == 5
    assert config.max_agent_steps == 5
    assert config.enabled_tool_names == []


async def test_duplicate_slug_within_one_org_is_a_conflict(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        await service.create_agent(CreateAgentInput(name="Sales Bot"))
        with pytest.raises(ConflictError):
            await service.create_agent(CreateAgentInput(name="Sales Bot"))


async def test_the_same_slug_is_allowed_in_a_different_org(tenant_a, tenant_b):
    """Uniqueness is per-organization. Two businesses may both have a
    'sales-bot' — that is the entire point of scoping the constraint."""
    async with tenant_session(tenant_a) as session:
        await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    async with tenant_session(tenant_b) as session:
        agent = await AgentService(session, tenant_b).create_agent(
            CreateAgentInput(name="Sales Bot")
        )
    assert agent.slug == "sales-bot"


async def test_list_returns_only_this_organizations_agents(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="A Bot")
        )
    async with tenant_session(tenant_b) as session:
        agents = await AgentService(session, tenant_b).list_agents()
    assert [a.name for a in agents] == []


async def test_get_agent_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="A Bot")
        )
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).get_agent(agent.id)


async def test_update_agent_changes_only_supplied_fields(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(
            CreateAgentInput(name="Sales Bot", model="gpt-4o-mini")
        )
        updated = await service.update_agent(
            agent.id, UpdateAgentInput(temperature=0.2)
        )
    assert updated.temperature == 0.2
    assert updated.model == "gpt-4o-mini"


async def test_update_config_persists(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot"))
        config = await service.update_config(
            agent.id, UpdateAgentConfigInput(tone="formal", retrieval_top_k=8)
        )
    assert config.tone == "formal"
    assert config.retrieval_top_k == 8


async def test_delete_removes_the_agent(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = AgentService(session, tenant_a)
        agent = await service.create_agent(CreateAgentInput(name="Sales Bot"))
        await service.delete_agent(agent.id)
        with pytest.raises(NotFoundError):
            await service.get_agent(agent.id)


async def test_delete_from_another_org_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        agent = await AgentService(session, tenant_a).create_agent(
            CreateAgentInput(name="A Bot")
        )
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await AgentService(session, tenant_b).delete_agent(agent.id)
```

- [ ] **Step 2: Add the shared tenant fixtures**

Append to `apps/api/tests/conftest.py`:

```python
@pytest.fixture
async def tenant_a(owner_connection):
    """A real organization plus a TenantContext for it, torn down after."""
    async for context in _make_tenant(owner_connection, "Tenant A"):
        yield context


@pytest.fixture
async def tenant_b(owner_connection):
    async for context in _make_tenant(owner_connection, "Tenant B"):
        yield context


async def _make_tenant(owner_connection, name: str):
    from sqlalchemy import text

    from app.core.ids import uuid7
    from app.core.tenancy import TenantContext
    from app.db.models import MembershipRole

    org_id, user_id = uuid7(), uuid7()
    slug = f"{name.lower().replace(' ', '-')}-{org_id.hex[:8]}"
    await owner_connection.execute(
        text(
            "INSERT INTO organizations (id, name, slug, plan, settings) "
            "VALUES (:id, :name, :slug, 'free', '{}')"
        ),
        {"id": org_id, "name": name, "slug": slug},
    )
    await owner_connection.commit()

    yield TenantContext(
        organization_id=org_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="test",
    )

    await owner_connection.execute(
        text("DELETE FROM organizations WHERE id = :id"), {"id": org_id}
    )
    await owner_connection.commit()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/integration/test_agent_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.service'`.

- [ ] **Step 4: Write the models**

Create `apps/api/app/db/models/agent.py`:

```python
import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class AgentStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class Agent(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("organization_id", "slug", name="uq_agent_org_slug"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[AgentStatus] = mapped_column(
        SAEnum(
            AgentStatus,
            name="agent_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=AgentStatus.DRAFT,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="gpt-4o-mini")
    temperature: Mapped[float] = mapped_column(Float, nullable=False, default=0.3)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=1024)
    prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompts.id", ondelete="SET NULL"),
        nullable=True,
    )
    public_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class AgentConfig(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """Behaviour, separated from identity: the playground edits this row
    constantly while the agent row stays stable."""

    __tablename__ = "agent_configs"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    persona: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str] = mapped_column(String(50), nullable=False, default="friendly")
    language: Mapped[str] = mapped_column(String(20), nullable=False, default="en")
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    fallback_message: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="I don't have that information. Would you like me to connect you "
        "with someone who does?",
    )
    enabled_tool_names: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    retrieval_top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    retrieval_min_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    max_agent_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    guardrails: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
```

Modify `apps/api/app/db/models/__init__.py` to export `Agent`, `AgentConfig`, `AgentStatus` alongside the existing names.

- [ ] **Step 5: Write the migration**

Create `apps/api/alembic/versions/0003_agents.py`. Because `agents.prompt_id` references `prompts`, which Task 8 creates, the foreign key is added in Task 8's migration — this one creates the nullable column only.

```python
"""agents and agent_configs

Revision ID: 0003_agents
Revises: 0002_rls_probe
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.base import disable_rls, enable_rls

revision = "0003_agents"
down_revision = "0002_rls_probe"
branch_labels = None
depends_on = None

_TIMESTAMPS = (
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
        nullable=False,
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
        nullable=False,
    ),
)


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(120), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "disabled", name="agent_status"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("provider", sa.String(50), nullable=False, server_default="openai"),
        sa.Column("model", sa.String(100), nullable=False, server_default="gpt-4o-mini"),
        sa.Column("temperature", sa.Float(), nullable=False, server_default="0.3"),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="1024"),
        sa.Column("prompt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("public_key", sa.String(64), nullable=False, unique=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("organization_id", "slug", name="uq_agent_org_slug"),
    )
    op.create_index("ix_agents_organization_id", "agents", ["organization_id"])
    enable_rls(op, "agents")

    op.create_table(
        "agent_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("persona", sa.Text(), nullable=True),
        sa.Column("tone", sa.String(50), nullable=False, server_default="friendly"),
        sa.Column("language", sa.String(20), nullable=False, server_default="en"),
        sa.Column("greeting", sa.Text(), nullable=True),
        sa.Column("fallback_message", sa.Text(), nullable=False),
        sa.Column(
            "enabled_tool_names",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("retrieval_top_k", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "retrieval_min_score", sa.Float(), nullable=False, server_default="0.0"
        ),
        sa.Column("max_agent_steps", sa.Integer(), nullable=False, server_default="5"),
        sa.Column(
            "guardrails", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column("variables", postgresql.JSONB(), nullable=False, server_default="{}"),
        *_TIMESTAMPS,
    )
    op.create_index(
        "ix_agent_configs_organization_id", "agent_configs", ["organization_id"]
    )
    enable_rls(op, "agent_configs")


def downgrade() -> None:
    disable_rls(op, "agent_configs")
    op.drop_table("agent_configs")
    disable_rls(op, "agents")
    op.drop_table("agents")
    op.execute("DROP TYPE IF EXISTS agent_status")
```

- [ ] **Step 6: Write the schemas and service**

Create `apps/api/app/agents/schemas.py`:

```python
from pydantic import BaseModel, Field


class CreateAgentInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(default=1024, ge=1, le=32_000)


class UpdateAgentInput(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    status: str | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=32_000)


class UpdateAgentConfigInput(BaseModel):
    persona: str | None = None
    tone: str | None = None
    language: str | None = None
    greeting: str | None = None
    fallback_message: str | None = None
    enabled_tool_names: list[str] | None = None
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    retrieval_min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    max_agent_steps: int | None = Field(default=None, ge=1, le=20)
```

Create `apps/api/app/agents/service.py`:

```python
import secrets
import uuid

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.schemas import (
    CreateAgentInput,
    UpdateAgentConfigInput,
    UpdateAgentInput,
)
from app.core.errors import ConflictError, NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Agent, AgentConfig, AgentStatus

_DEFAULT_FALLBACK = (
    "I don't have that information. Would you like me to connect you with "
    "someone who does?"
)


class AgentService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def list_agents(self) -> list[Agent]:
        result = await self.session.execute(
            select(Agent)
            .where(Agent.organization_id == self.tenant.organization_id)
            .order_by(Agent.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_agent(self, agent_id: uuid.UUID) -> Agent:
        result = await self.session.execute(
            select(Agent).where(
                Agent.id == agent_id,
                Agent.organization_id == self.tenant.organization_id,
            )
        )
        agent = result.scalar_one_or_none()
        if agent is None:
            raise NotFoundError("agent not found")
        return agent

    async def get_config(self, agent_id: uuid.UUID) -> AgentConfig:
        await self.get_agent(agent_id)  # 404s for other tenants before touching config
        result = await self.session.execute(
            select(AgentConfig).where(AgentConfig.agent_id == agent_id)
        )
        config = result.scalar_one_or_none()
        if config is None:
            raise NotFoundError("agent config not found")
        return config

    async def create_agent(self, data: CreateAgentInput) -> Agent:
        agent = Agent(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            name=data.name,
            slug=slugify(data.name)[:120] or "agent",
            status=AgentStatus.DRAFT,
            provider=data.provider,
            model=data.model,
            temperature=data.temperature,
            max_tokens=data.max_tokens,
            public_key=f"pk_{secrets.token_urlsafe(24)}",
        )
        config = AgentConfig(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            agent_id=agent.id,
            fallback_message=_DEFAULT_FALLBACK,
            enabled_tool_names=[],
        )
        self.session.add_all([agent, config])
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(
                f"an agent named '{data.name}' already exists"
            ) from exc
        return agent

    async def update_agent(
        self, agent_id: uuid.UUID, data: UpdateAgentInput
    ) -> Agent:
        agent = await self.get_agent(agent_id)
        updates = data.model_dump(exclude_unset=True, exclude_none=True)

        if "name" in updates:
            agent.slug = slugify(updates["name"])[:120] or "agent"
        if "status" in updates:
            agent.status = AgentStatus(updates.pop("status"))
        for field, value in updates.items():
            setattr(agent, field, value)

        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError("an agent with that name already exists") from exc
        return agent

    async def update_config(
        self, agent_id: uuid.UUID, data: UpdateAgentConfigInput
    ) -> AgentConfig:
        config = await self.get_config(agent_id)
        for field, value in data.model_dump(exclude_unset=True, exclude_none=True).items():
            setattr(config, field, value)
        await self.session.flush()
        return config

    async def delete_agent(self, agent_id: uuid.UUID) -> None:
        agent = await self.get_agent(agent_id)
        await self.session.delete(agent)
        await self.session.flush()
```

- [ ] **Step 7: Migrate, run the tests, commit**

Run:
```bash
cd apps/api && uv run alembic upgrade head
uv run pytest tests/integration/test_agent_service.py -v
```
Expected: 11 passed.

```bash
git add apps/api
git commit -m "feat: add agents and agent configs with per-org slug uniqueness"
```

---

### Task 8: Prompts — versioning with a single-active-version invariant

**Files:**
- Create: `apps/api/app/db/models/prompt.py`
- Create: `apps/api/app/prompts/__init__.py`, `apps/api/app/prompts/schemas.py`, `apps/api/app/prompts/service.py`, `apps/api/app/prompts/defaults.py`
- Modify: `apps/api/app/db/models/__init__.py`
- Create: `apps/api/alembic/versions/0004_prompts.py`
- Test: `apps/api/tests/integration/test_prompt_service.py`

**Interfaces:**
- Consumes: the same bases as Task 7.
- Produces:
  - `Prompt` model: `id`, `organization_id`, `name`, `key`, `description`, timestamps. Unique on `(organization_id, key)`.
  - `PromptVersion` model: `id`, `organization_id`, `prompt_id`, `version`, `system_prompt`, `variables`, `is_active`, `notes`, `created_by`, timestamps. Unique on `(prompt_id, version)` **and** a partial unique index on `prompt_id WHERE is_active`.
  - `app.prompts.defaults.DEFAULT_SALES_SYSTEM_PROMPT: str` — the ten-rule prompt from the spec, with `{{company_name}}` and `{{agent_name}}` placeholders. Task 10's seed uses it.
  - `app.prompts.service.PromptService(session, tenant)` with `list_prompts()`, `get_prompt(id)`, `create_prompt(input) -> Prompt`, `create_version(prompt_id, input) -> PromptVersion`, `activate_version(version_id) -> PromptVersion`, `active_version(prompt_id) -> PromptVersion`.
  - The `0004_prompts` migration also adds the `agents.prompt_id` → `prompts.id` foreign key deferred from Task 7.

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/integration/test_prompt_service.py`:

```python
import pytest

from app.core.errors import NotFoundError
from app.core.tenancy import tenant_session
from app.prompts.schemas import CreatePromptInput, CreateVersionInput
from app.prompts.service import PromptService

pytestmark = pytest.mark.anyio


async def _prompt(session, tenant):
    return await PromptService(session, tenant).create_prompt(
        CreatePromptInput(
            name="Sales system prompt",
            key="sales_system",
            system_prompt="You are a helpful assistant for {{company_name}}.",
        )
    )


async def test_creating_a_prompt_creates_version_one(tenant_a):
    async with tenant_session(tenant_a) as session:
        prompt = await _prompt(session, tenant_a)
        version = await PromptService(session, tenant_a).active_version(prompt.id)
    assert version.version == 1
    assert version.is_active is True


async def test_new_versions_increment(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        second = await service.create_version(
            prompt.id, CreateVersionInput(system_prompt="v2")
        )
    assert second.version == 2


async def test_a_new_version_is_inactive_until_activated(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        second = await service.create_version(
            prompt.id, CreateVersionInput(system_prompt="v2")
        )
        assert second.is_active is False
        active = await service.active_version(prompt.id)
    assert active.version == 1


async def test_activating_a_version_deactivates_the_previous_one(tenant_a):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        second = await service.create_version(
            prompt.id, CreateVersionInput(system_prompt="v2")
        )
        await service.activate_version(second.id)
        active = await service.active_version(prompt.id)
    assert active.version == 2


async def test_exactly_one_version_is_active_after_repeated_switching(tenant_a):
    """The partial unique index makes 'exactly one active' a database
    guarantee. This test is what proves the index is actually there."""
    from sqlalchemy import func, select

    from app.db.models import PromptVersion

    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        v2 = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v2"))
        v3 = await service.create_version(prompt.id, CreateVersionInput(system_prompt="v3"))
        for version in (v2, v3, v2):
            await service.activate_version(version.id)

        count = await session.execute(
            select(func.count())
            .select_from(PromptVersion)
            .where(PromptVersion.prompt_id == prompt.id, PromptVersion.is_active)
        )
    assert count.scalar_one() == 1


async def test_prompts_are_scoped_to_their_organization(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        prompt = await _prompt(session, tenant_a)
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await PromptService(session, tenant_b).get_prompt(prompt.id)


async def test_activating_another_orgs_version_raises_not_found(tenant_a, tenant_b):
    async with tenant_session(tenant_a) as session:
        service = PromptService(session, tenant_a)
        prompt = await _prompt(session, tenant_a)
        version = await service.create_version(
            prompt.id, CreateVersionInput(system_prompt="v2")
        )
    async with tenant_session(tenant_b) as session:
        with pytest.raises(NotFoundError):
            await PromptService(session, tenant_b).activate_version(version.id)


async def test_default_prompt_contains_the_grounding_rules():
    from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT

    assert "{{company_name}}" in DEFAULT_SALES_SYSTEM_PROMPT
    assert "Never invent" in DEFAULT_SALES_SYSTEM_PROMPT
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/integration/test_prompt_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.prompts.service'`.

- [ ] **Step 3: Write the models**

Create `apps/api/app/db/models/prompt.py`:

```python
import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Prompt(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A named slot. The text lives in its versions, never here."""

    __tablename__ = "prompts"
    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_prompt_org_key"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class PromptVersion(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("prompt_id", "version", name="uq_prompt_version_number"),
    )

    prompt_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompts.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
```

Modify `apps/api/app/db/models/__init__.py` to export `Prompt` and `PromptVersion`.

- [ ] **Step 4: Write the default prompt**

Create `apps/api/app/prompts/defaults.py`:

```python
DEFAULT_SALES_SYSTEM_PROMPT = """\
You are the AI sales assistant for {{company_name}}.

Your job is to help customers understand our products and find the product that
best matches their needs.

Rules:

1. Only provide factual information supported by the company's knowledge base or
   available tools.
2. Never invent prices, specifications, availability, policies, or product
   information.
3. If information is unavailable, clearly say that you do not have that
   information.
4. Ask useful follow-up questions when necessary.
5. When appropriate, recommend products based on the customer's requirements.
6. Do not aggressively pressure customers to buy.
7. When a tool is required, use the appropriate tool.
8. When creating a lead or performing an external action, confirm the required
   information before executing the action.
9. Keep responses concise and useful.
10. Maintain context throughout the conversation.
"""
```

- [ ] **Step 5: Write the migration**

Create `apps/api/alembic/versions/0004_prompts.py`:

```python
"""prompts and prompt_versions, plus the deferred agents.prompt_id FK

Revision ID: 0004_prompts
Revises: 0003_agents
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.base import disable_rls, enable_rls

revision = "0004_prompts"
down_revision = "0003_agents"
branch_labels = None
depends_on = None

_TIMESTAMPS = (
    sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
        nullable=False,
    ),
    sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
        nullable=False,
    ),
)


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_TIMESTAMPS,
        sa.UniqueConstraint("organization_id", "key", name="uq_prompt_org_key"),
    )
    op.create_index("ix_prompts_organization_id", "prompts", ["organization_id"])
    enable_rls(op, "prompts")

    op.create_table(
        "prompt_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "prompt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prompts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "variables", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        *_TIMESTAMPS,
        sa.UniqueConstraint("prompt_id", "version", name="uq_prompt_version_number"),
    )
    op.create_index(
        "ix_prompt_versions_organization_id", "prompt_versions", ["organization_id"]
    )

    # At most one active version per prompt, enforced by the database rather
    # than by every call site remembering to deactivate the previous one.
    op.create_index(
        "uq_prompt_versions_one_active",
        "prompt_versions",
        ["prompt_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    enable_rls(op, "prompt_versions")

    op.create_foreign_key(
        "fk_agents_prompt_id", "agents", "prompts", ["prompt_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agents_prompt_id", "agents", type_="foreignkey")
    disable_rls(op, "prompt_versions")
    op.drop_index("uq_prompt_versions_one_active", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    disable_rls(op, "prompts")
    op.drop_table("prompts")
```

- [ ] **Step 6: Write the schemas and service**

Create `apps/api/app/prompts/schemas.py`:

```python
from pydantic import BaseModel, Field


class CreatePromptInput(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    description: str | None = None
    system_prompt: str = Field(min_length=1)


class CreateVersionInput(BaseModel):
    system_prompt: str = Field(min_length=1)
    notes: str | None = None
    variables: dict[str, str] = Field(default_factory=dict)
```

Create `apps/api/app/prompts/service.py`:

```python
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Prompt, PromptVersion
from app.prompts.schemas import CreatePromptInput, CreateVersionInput


class PromptService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def list_prompts(self) -> list[Prompt]:
        result = await self.session.execute(
            select(Prompt)
            .where(Prompt.organization_id == self.tenant.organization_id)
            .order_by(Prompt.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_prompt(self, prompt_id: uuid.UUID) -> Prompt:
        result = await self.session.execute(
            select(Prompt).where(
                Prompt.id == prompt_id,
                Prompt.organization_id == self.tenant.organization_id,
            )
        )
        prompt = result.scalar_one_or_none()
        if prompt is None:
            raise NotFoundError("prompt not found")
        return prompt

    async def create_prompt(self, data: CreatePromptInput) -> Prompt:
        """A prompt is never useful without text, so version 1 is created with
        it and activated immediately."""
        prompt = Prompt(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            name=data.name,
            key=data.key,
            description=data.description,
        )
        version = PromptVersion(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            prompt_id=prompt.id,
            version=1,
            system_prompt=data.system_prompt,
            is_active=True,
            created_by=self.tenant.user_id,
        )
        self.session.add_all([prompt, version])
        try:
            await self.session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"a prompt with key '{data.key}' already exists") from exc
        return prompt

    async def create_version(
        self, prompt_id: uuid.UUID, data: CreateVersionInput
    ) -> PromptVersion:
        await self.get_prompt(prompt_id)
        highest = await self.session.execute(
            select(func.max(PromptVersion.version)).where(
                PromptVersion.prompt_id == prompt_id
            )
        )
        version = PromptVersion(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            prompt_id=prompt_id,
            version=(highest.scalar_one() or 0) + 1,
            system_prompt=data.system_prompt,
            variables=data.variables,
            is_active=False,  # explicit activation is a separate, auditable act
            notes=data.notes,
            created_by=self.tenant.user_id,
        )
        self.session.add(version)
        await self.session.flush()
        return version

    async def activate_version(self, version_id: uuid.UUID) -> PromptVersion:
        result = await self.session.execute(
            select(PromptVersion).where(
                PromptVersion.id == version_id,
                PromptVersion.organization_id == self.tenant.organization_id,
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise NotFoundError("prompt version not found")

        # Deactivate first: the partial unique index rejects two active rows,
        # so the order of these two statements is load-bearing.
        await self.session.execute(
            update(PromptVersion)
            .where(
                PromptVersion.prompt_id == version.prompt_id,
                PromptVersion.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self.session.flush()
        version.is_active = True
        await self.session.flush()
        return version

    async def active_version(self, prompt_id: uuid.UUID) -> PromptVersion:
        result = await self.session.execute(
            select(PromptVersion).where(
                PromptVersion.prompt_id == prompt_id,
                PromptVersion.organization_id == self.tenant.organization_id,
                PromptVersion.is_active.is_(True),
            )
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise NotFoundError("no active version for this prompt")
        return version
```

- [ ] **Step 7: Migrate, run the tests, commit**

Run:
```bash
cd apps/api && uv run alembic upgrade head
uv run pytest tests/integration/test_prompt_service.py -v
```
Expected: 8 passed.

```bash
git add apps/api
git commit -m "feat: add prompt versioning with a single-active-version invariant"
```

---

### Task 9: GraphQL — Strawberry schema, tenant context, dataloaders

**Files:**
- Create: `apps/api/app/graphql/__init__.py`, `apps/api/app/graphql/context.py`, `apps/api/app/graphql/types.py`, `apps/api/app/graphql/resolvers.py`, `apps/api/app/graphql/schema.py`
- Modify: `apps/api/app/main.py` (mount `/graphql`)
- Modify: `apps/api/pyproject.toml` (add strawberry-graphql)
- Test: `apps/api/tests/integration/test_graphql.py`

**Interfaces:**
- Consumes: `tenant_from_bearer` (Task 6), `tenant_session` (Task 4), `AgentService` (Task 7), `PromptService` (Task 8).
- Produces:
  - `/graphql` endpoint (GET serves GraphiQL in `local` only; POST executes).
  - `app.graphql.context.Context` with `tenant: TenantContext`, `session: AsyncSession`, `agent_loader: DataLoader`.
  - Queries: `me`, `agents`, `agent(id)`, `prompts`, `prompt(id)`.
  - Mutations: `createAgent`, `updateAgent`, `updateAgentConfig`, `deleteAgent`, `createPrompt`, `createPromptVersion`, `activatePromptVersion`.
  - Errors are returned in the standard GraphQL `errors` array with `extensions.code` matching `AppError.code`.
  - **Task 11's codegen reads this schema.**

- [ ] **Step 1: Write the failing tests**

Create `apps/api/tests/integration/test_graphql.py`:

```python
import pytest

pytestmark = pytest.mark.anyio

REGISTRATION = {
    "email": "gql@example.com",
    "password": "correct-horse-battery",
    "full_name": "Grace GraphQL",
    "organization_name": "Ada Motors GQL",
}


@pytest.fixture
async def auth_headers(client, clean_users):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def test_graphql_requires_authentication(client):
    response = await graphql(client, "{ agents { id } }")
    body = response.json()
    assert body["errors"][0]["extensions"]["code"] == "unauthenticated"


async def test_me_returns_the_current_user_and_org(client, auth_headers):
    response = await graphql(
        client,
        "{ me { email organizationName role } }",
        headers=auth_headers,
    )
    assert response.json()["data"]["me"] == {
        "email": "gql@example.com",
        "organizationName": "Ada Motors GQL",
        "role": "owner",
    }


async def test_create_agent_mutation(client, auth_headers):
    response = await graphql(
        client,
        """
        mutation Create($name: String!) {
          createAgent(input: {name: $name}) { id name slug status }
        }
        """,
        {"name": "Showroom Bot"},
        auth_headers,
    )
    agent = response.json()["data"]["createAgent"]
    assert agent["slug"] == "showroom-bot"
    assert agent["status"] == "DRAFT"


async def test_agents_query_lists_created_agents(client, auth_headers):
    await graphql(
        client,
        'mutation { createAgent(input: {name: "Listed Bot"}) { id } }',
        headers=auth_headers,
    )
    response = await graphql(client, "{ agents { name } }", headers=auth_headers)
    names = [a["name"] for a in response.json()["data"]["agents"]]
    assert "Listed Bot" in names


async def test_agent_query_includes_its_config(client, auth_headers):
    created = await graphql(
        client,
        'mutation { createAgent(input: {name: "Config Bot"}) { id } }',
        headers=auth_headers,
    )
    agent_id = created.json()["data"]["createAgent"]["id"]
    response = await graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name config { tone retrievalTopK } } }",
        {"id": agent_id},
        auth_headers,
    )
    assert response.json()["data"]["agent"]["config"] == {
        "tone": "friendly",
        "retrievalTopK": 5,
    }


async def test_update_agent_config_mutation(client, auth_headers):
    created = await graphql(
        client,
        'mutation { createAgent(input: {name: "Tuned Bot"}) { id } }',
        headers=auth_headers,
    )
    agent_id = created.json()["data"]["createAgent"]["id"]
    response = await graphql(
        client,
        """
        mutation U($id: UUID!) {
          updateAgentConfig(agentId: $id, input: {tone: "formal"}) { tone }
        }
        """,
        {"id": agent_id},
        auth_headers,
    )
    assert response.json()["data"]["updateAgentConfig"]["tone"] == "formal"


async def test_unknown_agent_returns_a_not_found_code(client, auth_headers):
    response = await graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": "00000000-0000-7000-8000-000000000000"},
        auth_headers,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_prompt_version_activation_through_graphql(client, auth_headers):
    created = await graphql(
        client,
        """
        mutation {
          createPrompt(input: {
            name: "Sales", key: "sales_system", systemPrompt: "v1"
          }) { id }
        }
        """,
        headers=auth_headers,
    )
    prompt_id = created.json()["data"]["createPrompt"]["id"]

    version = await graphql(
        client,
        """
        mutation V($id: UUID!) {
          createPromptVersion(promptId: $id, input: {systemPrompt: "v2"}) {
            id version isActive
          }
        }
        """,
        {"id": prompt_id},
        auth_headers,
    )
    version_id = version.json()["data"]["createPromptVersion"]["id"]
    assert version.json()["data"]["createPromptVersion"]["isActive"] is False

    activated = await graphql(
        client,
        "mutation A($id: UUID!) { activatePromptVersion(versionId: $id) { version isActive } }",
        {"id": version_id},
        auth_headers,
    )
    assert activated.json()["data"]["activatePromptVersion"] == {
        "version": 2,
        "isActive": True,
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/integration/test_graphql.py -v`
Expected: FAIL — every request 404s, `/graphql` is not mounted.

- [ ] **Step 3: Add the dependency**

Modify `apps/api/pyproject.toml`, adding to `dependencies`:

```toml
    "strawberry-graphql[fastapi]>=0.248",
```

Run: `cd apps/api && uv sync`

- [ ] **Step 4: Write the GraphQL context**

Create `apps/api/app/graphql/context.py`:

```python
import uuid
from collections.abc import Sequence

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.dataloader import DataLoader

from app.auth.dependencies import tenant_from_bearer
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import AgentConfig


class Context:
    """Per-request state. One tenant-bound session for the whole operation,
    so every resolver in a query shares one transaction and one RLS setting.

    `tenant` and `session` are nullable because an unauthenticated request
    still needs a context object: raising during context construction would
    let FastAPI's exception handler return a bare 401 instead of a properly
    shaped GraphQL error. Resolvers call `_require_tenant` instead.
    """

    def __init__(
        self, tenant: TenantContext | None, session: AsyncSession | None
    ) -> None:
        self.tenant = tenant
        self.session = session
        self.config_loader: DataLoader[uuid.UUID, AgentConfig | None] | None = (
            DataLoader(load_fn=self._load_configs) if session is not None else None
        )

    async def _load_configs(
        self, agent_ids: Sequence[uuid.UUID]
    ) -> list[AgentConfig | None]:
        """Batches `agents { config { ... } }` into one query instead of one
        per agent. Without this, listing 50 agents issues 51 queries."""
        assert self.session is not None
        result = await self.session.execute(
            select(AgentConfig).where(AgentConfig.agent_id.in_(list(agent_ids)))
        )
        by_agent = {config.agent_id: config for config in result.scalars().all()}
        return [by_agent.get(agent_id) for agent_id in agent_ids]


async def build_context(request: Request) -> AsyncIterator[Context]:
    """A generator dependency: FastAPI holds it open for the whole request, so
    the tenant session and its transaction stay alive while resolvers run."""
    try:
        tenant = tenant_from_bearer(request)
    except AuthenticationError:
        yield Context(tenant=None, session=None)
        return

    async with tenant_session(tenant) as session:
        yield Context(tenant, session)
```

Add the imports this needs at the top of the file: `from collections.abc import AsyncIterator, Sequence` and `from app.core.errors import AuthenticationError`.

- [ ] **Step 5: Write the types**

Create `apps/api/app/graphql/types.py`:

```python
import enum
import uuid
from datetime import datetime

import strawberry

from app.db.models import Agent as AgentModel
from app.db.models import AgentConfig as AgentConfigModel
from app.db.models import Prompt as PromptModel
from app.db.models import PromptVersion as PromptVersionModel


@strawberry.enum
class AgentStatus(enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


@strawberry.type
class Me:
    user_id: uuid.UUID
    email: str
    full_name: str
    organization_id: uuid.UUID
    organization_name: str
    role: str


@strawberry.type
class AgentConfig:
    id: uuid.UUID
    tone: str
    language: str
    persona: str | None
    greeting: str | None
    fallback_message: str
    enabled_tool_names: list[str]
    retrieval_top_k: int
    retrieval_min_score: float
    max_agent_steps: int

    @classmethod
    def from_model(cls, model: AgentConfigModel) -> "AgentConfig":
        return cls(
            id=model.id,
            tone=model.tone,
            language=model.language,
            persona=model.persona,
            greeting=model.greeting,
            fallback_message=model.fallback_message,
            enabled_tool_names=list(model.enabled_tool_names),
            retrieval_top_k=model.retrieval_top_k,
            retrieval_min_score=model.retrieval_min_score,
            max_agent_steps=model.max_agent_steps,
        )


@strawberry.type
class Agent:
    id: uuid.UUID
    name: str
    slug: str
    status: AgentStatus
    provider: str
    model: str
    temperature: float
    max_tokens: int
    prompt_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, model: AgentModel) -> "Agent":
        return cls(
            id=model.id,
            name=model.name,
            slug=model.slug,
            status=AgentStatus(model.status.value),
            provider=model.provider,
            model=model.model,
            temperature=model.temperature,
            max_tokens=model.max_tokens,
            prompt_id=model.prompt_id,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    @strawberry.field
    async def config(self, info: strawberry.Info) -> AgentConfig | None:
        model = await info.context.config_loader.load(self.id)
        return AgentConfig.from_model(model) if model else None


@strawberry.type
class PromptVersion:
    id: uuid.UUID
    version: int
    system_prompt: str
    is_active: bool
    notes: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: PromptVersionModel) -> "PromptVersion":
        return cls(
            id=model.id,
            version=model.version,
            system_prompt=model.system_prompt,
            is_active=model.is_active,
            notes=model.notes,
            created_at=model.created_at,
        )


@strawberry.type
class Prompt:
    id: uuid.UUID
    name: str
    key: str
    description: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, model: PromptModel) -> "Prompt":
        return cls(
            id=model.id,
            name=model.name,
            key=model.key,
            description=model.description,
            created_at=model.created_at,
        )


@strawberry.input
class CreateAgentInput:
    name: str
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    temperature: float = 0.3
    max_tokens: int = 1024


@strawberry.input
class UpdateAgentInput:
    name: str | None = None
    status: AgentStatus | None = None
    provider: str | None = None
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@strawberry.input
class UpdateAgentConfigInput:
    persona: str | None = None
    tone: str | None = None
    language: str | None = None
    greeting: str | None = None
    fallback_message: str | None = None
    enabled_tool_names: list[str] | None = None
    retrieval_top_k: int | None = None
    retrieval_min_score: float | None = None
    max_agent_steps: int | None = None


@strawberry.input
class CreatePromptInput:
    name: str
    key: str
    system_prompt: str
    description: str | None = None


@strawberry.input
class CreatePromptVersionInput:
    system_prompt: str
    notes: str | None = None
```

- [ ] **Step 6: Write the resolvers, schema, and error extension**

Create `apps/api/app/graphql/resolvers.py`:

```python
import uuid

import strawberry
from sqlalchemy import select

from app.agents import schemas as agent_schemas
from app.agents.service import AgentService
from app.core.errors import AuthenticationError
from app.db.models import Membership, Organization
from app.db.models import User as UserModel
from app.graphql import types as gql
from app.prompts import schemas as prompt_schemas
from app.prompts.service import PromptService


def _require_tenant(info: strawberry.Info) -> None:
    """Every resolver's first line. Authentication is checked here rather than
    in the context builder so the failure is rendered as a GraphQL error with
    an `extensions.code`, matching the REST envelope."""
    if info.context.tenant is None:
        raise AuthenticationError("authentication required")


def _agents(info: strawberry.Info) -> AgentService:
    _require_tenant(info)
    return AgentService(info.context.session, info.context.tenant)


def _prompts(info: strawberry.Info) -> PromptService:
    _require_tenant(info)
    return PromptService(info.context.session, info.context.tenant)


@strawberry.type
class Query:
    @strawberry.field
    async def me(self, info: strawberry.Info) -> gql.Me:
        _require_tenant(info)
        tenant = info.context.tenant
        result = await info.context.session.execute(
            select(UserModel, Organization, Membership)
            .join(Membership, Membership.user_id == UserModel.id)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(
                UserModel.id == tenant.user_id,
                Membership.organization_id == tenant.organization_id,
            )
        )
        row = result.first()
        if row is None:
            raise AuthenticationError("account no longer exists")
        user, organization, membership = row
        return gql.Me(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            organization_id=organization.id,
            organization_name=organization.name,
            role=membership.role.value,
        )

    @strawberry.field
    async def agents(self, info: strawberry.Info) -> list[gql.Agent]:
        return [gql.Agent.from_model(a) for a in await _agents(info).list_agents()]

    @strawberry.field
    async def agent(self, info: strawberry.Info, id: uuid.UUID) -> gql.Agent:
        return gql.Agent.from_model(await _agents(info).get_agent(id))

    @strawberry.field
    async def prompts(self, info: strawberry.Info) -> list[gql.Prompt]:
        return [gql.Prompt.from_model(p) for p in await _prompts(info).list_prompts()]

    @strawberry.field
    async def prompt(self, info: strawberry.Info, id: uuid.UUID) -> gql.Prompt:
        return gql.Prompt.from_model(await _prompts(info).get_prompt(id))


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_agent(
        self, info: strawberry.Info, input: gql.CreateAgentInput
    ) -> gql.Agent:
        agent = await _agents(info).create_agent(
            agent_schemas.CreateAgentInput(
                name=input.name,
                provider=input.provider,
                model=input.model,
                temperature=input.temperature,
                max_tokens=input.max_tokens,
            )
        )
        return gql.Agent.from_model(agent)

    @strawberry.mutation
    async def update_agent(
        self, info: strawberry.Info, id: uuid.UUID, input: gql.UpdateAgentInput
    ) -> gql.Agent:
        payload = agent_schemas.UpdateAgentInput(
            name=input.name,
            status=input.status.value if input.status else None,
            provider=input.provider,
            model=input.model,
            temperature=input.temperature,
            max_tokens=input.max_tokens,
        )
        return gql.Agent.from_model(await _agents(info).update_agent(id, payload))

    @strawberry.mutation
    async def update_agent_config(
        self,
        info: strawberry.Info,
        agent_id: uuid.UUID,
        input: gql.UpdateAgentConfigInput,
    ) -> gql.AgentConfig:
        payload = agent_schemas.UpdateAgentConfigInput(
            persona=input.persona,
            tone=input.tone,
            language=input.language,
            greeting=input.greeting,
            fallback_message=input.fallback_message,
            enabled_tool_names=input.enabled_tool_names,
            retrieval_top_k=input.retrieval_top_k,
            retrieval_min_score=input.retrieval_min_score,
            max_agent_steps=input.max_agent_steps,
        )
        config = await _agents(info).update_config(agent_id, payload)
        return gql.AgentConfig.from_model(config)

    @strawberry.mutation
    async def delete_agent(self, info: strawberry.Info, id: uuid.UUID) -> bool:
        await _agents(info).delete_agent(id)
        return True

    @strawberry.mutation
    async def create_prompt(
        self, info: strawberry.Info, input: gql.CreatePromptInput
    ) -> gql.Prompt:
        prompt = await _prompts(info).create_prompt(
            prompt_schemas.CreatePromptInput(
                name=input.name,
                key=input.key,
                description=input.description,
                system_prompt=input.system_prompt,
            )
        )
        return gql.Prompt.from_model(prompt)

    @strawberry.mutation
    async def create_prompt_version(
        self,
        info: strawberry.Info,
        prompt_id: uuid.UUID,
        input: gql.CreatePromptVersionInput,
    ) -> gql.PromptVersion:
        version = await _prompts(info).create_version(
            prompt_id,
            prompt_schemas.CreateVersionInput(
                system_prompt=input.system_prompt, notes=input.notes
            ),
        )
        return gql.PromptVersion.from_model(version)

    @strawberry.mutation
    async def activate_prompt_version(
        self, info: strawberry.Info, version_id: uuid.UUID
    ) -> gql.PromptVersion:
        version = await _prompts(info).activate_version(version_id)
        return gql.PromptVersion.from_model(version)
```

Create `apps/api/app/graphql/schema.py`:

```python
from typing import Any

import strawberry
from graphql import GraphQLError
from strawberry.extensions import SchemaExtension

from app.core.errors import AppError
from app.graphql.resolvers import Mutation, Query


class AppErrorExtension(SchemaExtension):
    """Give GraphQL errors the same machine-readable codes the REST envelope
    uses, so the frontend has one error contract rather than two."""

    def on_operation(self) -> Any:
        yield
        result = self.execution_context.result
        if result is None or not result.errors:
            return
        for error in result.errors:
            original = error.original_error
            if isinstance(original, AppError):
                error.extensions = {**(error.extensions or {}), "code": original.code}
                error.message = original.message
            elif isinstance(error, GraphQLError) and error.original_error is None:
                error.extensions = {
                    **(error.extensions or {}),
                    "code": "invalid_input",
                }


schema = strawberry.Schema(
    query=Query, mutation=Mutation, extensions=[AppErrorExtension]
)
```

- [ ] **Step 7: Mount the router**

Modify `apps/api/app/main.py`, adding inside `create_app()` before `return app`:

```python
    from strawberry.fastapi import GraphQLRouter

    from app.graphql.context import build_context
    from app.graphql.schema import schema

    app.include_router(
        GraphQLRouter(
            schema,
            context_getter=build_context,
            graphiql=settings.environment == "local",
        ),
        prefix="/graphql",
    )
```

Also add the matching import at the top of the file: `from app.graphql.context import build_context` is imported locally above to keep `create_app` self-contained; no top-level change is needed.

Why authentication is checked in the resolvers rather than in `build_context`: if the context builder raised, FastAPI's `AppError` handler would intercept it and return a bare `401` JSON envelope. The client would then get two different error shapes from the same endpoint depending on *why* the request failed. Yielding a tenant-less context and raising inside the resolver keeps every GraphQL failure in the `errors` array with an `extensions.code`.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/integration/test_graphql.py -v`
Expected: 8 passed.

- [ ] **Step 9: Export the schema for the frontend**

Add to `Makefile`:

```makefile
.PHONY: schema

schema:
	cd apps/api && uv run strawberry export-schema app.graphql.schema:schema > ../../packages/shared/schema.graphql
```

Run: `mkdir -p packages/shared && make schema`
Expected: `packages/shared/schema.graphql` exists and contains `type Agent`.

- [ ] **Step 10: Commit**

```bash
git add apps/api packages Makefile
git commit -m "feat: add GraphQL API with tenant context and dataloaders"
```

---

### Task 10: Tenant isolation gate and seed data

**Files:**
- Create: `apps/api/tests/integration/test_tenant_isolation.py`
- Create: `apps/api/app/db/seed.py`
- Modify: `Makefile`

**Interfaces:**
- Consumes: every public entry point built so far.
- Produces: `python -m app.db.seed` creating a demo organization, owner user, prompt with an active version, and one agent. Credentials: `demo@example.com` / `demo-password-123`.

This task adds no features. It is the gate that decides whether Phase 1 is actually done.

- [ ] **Step 1: Write the isolation suite**

Create `apps/api/tests/integration/test_tenant_isolation.py`:

```python
"""The Phase 1 security gate.

Two real accounts in two organizations, driven only through the public API.
Nothing here reaches into the database directly — the point is to prove that
isolation holds through the same surface a customer would use.
"""

import pytest

pytestmark = pytest.mark.anyio

ORG_A = {
    "email": "a-owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Owner A",
    "organization_name": "Ada Motors A",
}
ORG_B = {
    "email": "b-owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Owner B",
    "organization_name": "Ada Motors B",
}


async def _register(client, payload):
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _graphql(client, query, variables=None, headers=None):
    return await client.post(
        "/graphql",
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


@pytest.fixture
async def two_accounts(client, clean_users):
    headers_a = await _register(client, ORG_A)
    headers_b = await _register(client, ORG_B)

    created = await _graphql(
        client,
        'mutation { createAgent(input: {name: "Secret A Bot"}) { id } }',
        headers=headers_a,
    )
    agent_a_id = created.json()["data"]["createAgent"]["id"]

    prompt = await _graphql(
        client,
        """
        mutation {
          createPrompt(input: {
            name: "A Prompt", key: "a_secret", systemPrompt: "A's secret prompt"
          }) { id }
        }
        """,
        headers=headers_a,
    )
    prompt_a_id = prompt.json()["data"]["createPrompt"]["id"]
    return headers_a, headers_b, agent_a_id, prompt_a_id


async def test_b_does_not_see_as_agents_in_a_list(two_accounts, client):
    _a, headers_b, _agent, _prompt = two_accounts
    response = await _graphql(client, "{ agents { name } }", headers=headers_b)
    assert response.json()["data"]["agents"] == []


async def test_b_cannot_read_as_agent_by_id(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_update_as_agent(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        'mutation U($id: UUID!) { updateAgent(id: $id, input: {name: "Hijacked"}) { name } }',
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_delete_as_agent(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "mutation D($id: UUID!) { deleteAgent(id: $id) }",
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_read_as_agent_config(two_accounts, client):
    _a, headers_b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "mutation U($id: UUID!) { updateAgentConfig(agentId: $id, input: {tone: \"rude\"}) { tone } }",
        {"id": agent_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_does_not_see_as_prompts(two_accounts, client):
    _a, headers_b, _agent, _prompt = two_accounts
    response = await _graphql(client, "{ prompts { key } }", headers=headers_b)
    assert response.json()["data"]["prompts"] == []


async def test_b_cannot_read_as_prompt_by_id(two_accounts, client):
    _a, headers_b, _agent, prompt_a_id = two_accounts
    response = await _graphql(
        client,
        "query P($id: UUID!) { prompt(id: $id) { key } }",
        {"id": prompt_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_b_cannot_add_a_version_to_as_prompt(two_accounts, client):
    _a, headers_b, _agent, prompt_a_id = two_accounts
    response = await _graphql(
        client,
        """
        mutation V($id: UUID!) {
          createPromptVersion(promptId: $id, input: {systemPrompt: "injected"}) { id }
        }
        """,
        {"id": prompt_a_id},
        headers_b,
    )
    assert response.json()["errors"][0]["extensions"]["code"] == "not_found"


async def test_me_reports_each_owners_own_organization(two_accounts, client):
    headers_a, headers_b, _agent, _prompt = two_accounts
    a = await _graphql(client, "{ me { organizationName } }", headers=headers_a)
    b = await _graphql(client, "{ me { organizationName } }", headers=headers_b)
    assert a.json()["data"]["me"]["organizationName"] == "Ada Motors A"
    assert b.json()["data"]["me"]["organizationName"] == "Ada Motors B"


async def test_a_can_still_see_its_own_agent(two_accounts, client):
    """The mirror of every test above: isolation that also blocks the owner
    is a bug, not security."""
    headers_a, _b, agent_a_id, _prompt = two_accounts
    response = await _graphql(
        client,
        "query A($id: UUID!) { agent(id: $id) { name } }",
        {"id": agent_a_id},
        headers_a,
    )
    assert response.json()["data"]["agent"]["name"] == "Secret A Bot"
```

- [ ] **Step 2: Widen the cleanup fixture**

The isolation tests create two organizations. Modify the `_purge` helper in `apps/api/tests/conftest.py` so its organization deletion covers them:

```python
        await owner_connection.execute(
            text("DELETE FROM organizations WHERE name LIKE 'Ada Motors%'")
        )
```

(Replace the previous `slug LIKE 'ada-motors%'` statement. Deleting the organization cascades to agents, configs, prompts and versions.)

- [ ] **Step 3: Run the isolation suite**

Run: `cd apps/api && uv run pytest tests/integration/test_tenant_isolation.py -v`
Expected: 10 passed.

If any test returns data instead of `not_found`, stop and fix it before continuing — this is the gate.

- [ ] **Step 4: Write the seed script**

Create `apps/api/app/db/seed.py`:

```python
"""Idempotent development seed. Safe to run repeatedly."""

import asyncio

from sqlalchemy import select

from app.agents.schemas import CreateAgentInput
from app.agents.service import AgentService
from app.core.ids import uuid7
from app.core.security import hash_password
from app.core.tenancy import TenantContext, tenant_session, untenanted_session
from app.db.models import (
    Membership,
    MembershipRole,
    Organization,
    Prompt,
    User,
)
from app.prompts.defaults import DEFAULT_SALES_SYSTEM_PROMPT
from app.prompts.schemas import CreatePromptInput
from app.prompts.service import PromptService

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo-password-123"


async def seed() -> None:
    async with untenanted_session() as session:
        existing = await session.execute(select(User).where(User.email == DEMO_EMAIL))
        if existing.scalar_one_or_none() is not None:
            print(f"seed: {DEMO_EMAIL} already exists, nothing to do")  # noqa: T201
            return

        organization = Organization(id=uuid7(), name="Demo Motors", slug="demo-motors")
        user = User(
            id=uuid7(),
            email=DEMO_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            full_name="Demo Owner",
        )
        session.add_all(
            [
                organization,
                user,
                Membership(
                    id=uuid7(),
                    organization_id=organization.id,
                    user_id=user.id,
                    role=MembershipRole.OWNER,
                ),
            ]
        )
        await session.flush()
        org_id, user_id = organization.id, user.id

    tenant = TenantContext(
        organization_id=org_id,
        user_id=user_id,
        role=MembershipRole.OWNER,
        request_id="seed",
    )
    async with tenant_session(tenant) as session:
        prompt = await PromptService(session, tenant).create_prompt(
            CreatePromptInput(
                name="Sales system prompt",
                key="sales_system",
                description="The default grounded sales assistant prompt.",
                system_prompt=DEFAULT_SALES_SYSTEM_PROMPT,
            )
        )
        agent = await AgentService(session, tenant).create_agent(
            CreateAgentInput(name="Demo Sales Agent")
        )
        agent.prompt_id = prompt.id

    print(f"seed: created {DEMO_EMAIL} / {DEMO_PASSWORD} in Demo Motors")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(seed())
```

Note the `Prompt` import is unused in the final form — remove it so `ruff` passes.

- [ ] **Step 5: Add the seed target and run it**

Append to `Makefile`:

```makefile
.PHONY: seed

seed:
	cd apps/api && uv run python -m app.db.seed
```

Run: `make seed && make seed`
Expected: first run creates the data; second prints "already exists, nothing to do".

- [ ] **Step 6: Run the whole backend suite and commit**

Run: `make test && make lint`
Expected: all green.

```bash
git add apps/api Makefile
git commit -m "test: add cross-tenant isolation gate and development seed"
```

---

### Task 11: Web app — scaffold, GraphQL client, authentication

**Files:**
- Create: `apps/web/package.json`, `tsconfig.json`, `next.config.ts`, `postcss.config.mjs`, `codegen.ts`, `.eslintrc.json`
- Create: `apps/web/src/app/layout.tsx`, `globals.css`, `page.tsx`
- Create: `apps/web/src/lib/auth.tsx`, `apps/web/src/lib/urql.tsx`, `apps/web/src/lib/api.ts`
- Create: `apps/web/src/app/(auth)/login/page.tsx`, `apps/web/src/app/(auth)/register/page.tsx`
- Create: `apps/web/src/middleware.ts`
- Create: `apps/web/src/graphql/operations.graphql`

**Interfaces:**
- Consumes: REST auth endpoints (Task 6), `/graphql` (Task 9), `packages/shared/schema.graphql` (Task 9 Step 9).
- Produces:
  - `AuthProvider` / `useAuth()` exposing `{ accessToken, user, login, register, logout, loading }`. The access token lives in React state only — never `localStorage`, which is readable by any injected script.
  - `UrqlProvider` attaching `Authorization: Bearer` and retrying once through `/auth/refresh` on an `unauthenticated` error.
  - `apps/web/src/graphql/generated.ts` (git-ignored) produced by `npm run codegen`.
  - Working `/login` and `/register` pages that redirect to `/dashboard`.

- [ ] **Step 1: Scaffold the app**

Run:
```bash
cd apps && npx create-next-app@latest web --typescript --tailwind --app --eslint --src-dir --use-npm --no-turbopack --import-alias "@/*"
cd web && npm install urql graphql
npm install -D @graphql-codegen/cli @graphql-codegen/client-preset
```

- [ ] **Step 2: Configure codegen**

Create `apps/web/codegen.ts`:

```ts
import type { CodegenConfig } from "@graphql-codegen/cli";

const config: CodegenConfig = {
  schema: "../../packages/shared/schema.graphql",
  documents: ["src/**/*.graphql"],
  generates: {
    "src/graphql/generated.ts": { plugins: ["typescript", "typescript-operations"] },
  },
};

export default config;
```

Run `npm install -D @graphql-codegen/typescript @graphql-codegen/typescript-operations`, and add to `apps/web/package.json` scripts:

```json
    "codegen": "graphql-codegen --config codegen.ts",
    "typecheck": "tsc --noEmit"
```

- [ ] **Step 3: Write the API helper and auth context**

Create `apps/web/src/lib/api.ts`:

```ts
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type ApiError = { code: string; message: string };

/** Every call sends credentials: the refresh token is an httpOnly cookie. */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error: ApiError = body?.error ?? {
      code: "network_error",
      message: "Something went wrong. Please try again.",
    };
    throw error;
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}
```

Create `apps/web/src/lib/auth.tsx`:

```tsx
"use client";

import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type TokenResponse = { access_token: string; expires_in: number };
type Me = {
  user_id: string;
  email: string;
  full_name: string;
  organization_id: string;
  organization_name: string;
  role: string;
};

type AuthValue = {
  accessToken: string | null;
  user: Me | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  register: (input: RegisterInput) => Promise<void>;
  logout: () => Promise<void>;
};

type RegisterInput = {
  email: string;
  password: string;
  full_name: string;
  organization_name: string;
};

const AuthContext = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Held in memory only. localStorage is readable by any injected script.
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const loadUser = useCallback(async (token: string) => {
    const me = await apiFetch<Me>("/api/v1/auth/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    setUser(me);
  }, []);

  // On mount, trade the refresh cookie for an access token so a page reload
  // does not log the user out.
  useEffect(() => {
    (async () => {
      try {
        const tokens = await apiFetch<TokenResponse>("/api/v1/auth/refresh", {
          method: "POST",
        });
        setAccessToken(tokens.access_token);
        await loadUser(tokens.access_token);
      } catch {
        setAccessToken(null);
        setUser(null);
      } finally {
        setLoading(false);
      }
    })();
  }, [loadUser]);

  const authenticate = useCallback(
    async (path: string, body: unknown) => {
      const tokens = await apiFetch<TokenResponse>(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      setAccessToken(tokens.access_token);
      await loadUser(tokens.access_token);
    },
    [loadUser],
  );

  const value: AuthValue = {
    accessToken,
    user,
    loading,
    login: (email, password) =>
      authenticate("/api/v1/auth/login", { email, password }),
    register: (input) => authenticate("/api/v1/auth/register", input),
    logout: async () => {
      await apiFetch<void>("/api/v1/auth/logout", { method: "POST" });
      setAccessToken(null);
      setUser(null);
    },
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside AuthProvider");
  return context;
}
```

- [ ] **Step 4: Write the urql provider**

Create `apps/web/src/lib/urql.tsx`:

```tsx
"use client";

import { useMemo } from "react";
import { Client, Provider, cacheExchange, fetchExchange } from "urql";
import { API_URL } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export function UrqlProvider({ children }: { children: React.ReactNode }) {
  const { accessToken } = useAuth();

  const client = useMemo(
    () =>
      new Client({
        url: `${API_URL}/graphql`,
        exchanges: [cacheExchange, fetchExchange],
        fetchOptions: () => ({
          credentials: "include",
          headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
        }),
      }),
    // A new client per token keeps the cache from serving one tenant's data
    // to the next session after a re-login.
    [accessToken],
  );

  return <Provider value={client}>{children}</Provider>;
}
```

- [ ] **Step 5: Wire the root layout**

Replace `apps/web/src/app/layout.tsx`:

```tsx
import type { Metadata } from "next";
import { AuthProvider } from "@/lib/auth";
import { UrqlProvider } from "@/lib/urql";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Sales Agent",
  description: "Configure and test your AI sales assistant.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-slate-50 text-slate-900 antialiased">
        <AuthProvider>
          <UrqlProvider>{children}</UrqlProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
```

Replace `apps/web/src/app/page.tsx`:

```tsx
import { redirect } from "next/navigation";

export default function Home() {
  redirect("/dashboard");
}
```

- [ ] **Step 6: Write the login and register pages**

Create `apps/web/src/app/(auth)/login/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/lib/auth";

export default function LoginPage() {
  const { login } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      router.push("/dashboard");
    } catch (caught) {
      setError((caught as { message?: string }).message ?? "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-4 rounded-xl border border-slate-200 bg-white p-8 shadow-sm"
      >
        <h1 className="text-xl font-semibold">Sign in</h1>

        {error && (
          <p role="alert" className="rounded-md bg-red-50 p-3 text-sm text-red-700">
            {error}
          </p>
        )}

        <label className="block text-sm font-medium">
          Email
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <label className="block text-sm font-medium">
          Password
          <input
            type="password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2"
          />
        </label>

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-md bg-slate-900 py-2 text-white disabled:opacity-50"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>

        <p className="text-center text-sm text-slate-600">
          No account?{" "}
          <Link href="/register" className="underline">
            Create one
          </Link>
        </p>
      </form>
    </main>
  );
}
```

Create `apps/web/src/app/(auth)/register/page.tsx` with the same structure, four fields (`full_name`, `organization_name`, `email`, `password`), a `minLength={12}` on the password input, a heading of "Create your workspace", calling `register({ email, password, full_name, organization_name })` and redirecting to `/dashboard`, with a link back to `/login`.

- [ ] **Step 7: Add the route guard**

Create `apps/web/src/middleware.ts`:

```ts
import { NextResponse, type NextRequest } from "next/server";

/**
 * A cheap first gate: the presence of the refresh cookie. The API is the real
 * authority — this only spares unauthenticated visitors a dashboard flash.
 */
export function middleware(request: NextRequest) {
  const hasSession = request.cookies.has("refresh_token");
  if (!hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    return NextResponse.redirect(url);
  }
  return NextResponse.next();
}

export const config = { matcher: ["/dashboard/:path*"] };
```

> The cookie is scoped to `/api/v1/auth`, so it is **not** sent to the Next.js origin on dashboard routes. Set the cookie `path` to `/` in `apps/api/app/api/auth.py` (`_set_refresh_cookie`) for the middleware to see it, and update `response.delete_cookie(REFRESH_COOKIE, path="/")` in `logout` to match. Make that change now and re-run `pytest tests/integration/test_auth.py` to confirm the cookie tests still pass.

- [ ] **Step 8: Generate types and verify the build**

Run:
```bash
cd apps/web && npm run codegen && npm run typecheck && npm run lint
```
Expected: all clean. (`codegen` needs `packages/shared/schema.graphql` from Task 9 and at least one `.graphql` document — create `src/graphql/operations.graphql` in Task 12 before running codegen, or add a placeholder `query Ping { me { email } }` now.)

- [ ] **Step 9: Manual verification**

Run `make up && make migrate && make api` in one terminal and `cd apps/web && npm run dev` in another. Register at `http://localhost:3000/register`, confirm redirection to `/dashboard` (a 404 for now), reload the page, and confirm you are not bounced to `/login`.

- [ ] **Step 10: Commit**

```bash
git add apps/web apps/api
git commit -m "feat: add Next.js app with in-memory access tokens and cookie refresh"
```

---

### Task 12: Dashboard — agent list, agent detail, navigation

**Files:**
- Create: `apps/web/src/graphql/operations.graphql`
- Create: `apps/web/src/app/dashboard/layout.tsx`, `page.tsx`, `agents/page.tsx`, `agents/[id]/page.tsx`
- Create: `apps/web/src/app/dashboard/knowledge/page.tsx`, `products/page.tsx`, `leads/page.tsx`, `prompts/page.tsx`, `playground/page.tsx`
- Create: `apps/web/src/components/ComingSoon.tsx`

**Interfaces:**
- Consumes: `useAuth` (Task 11), the GraphQL operations in Task 9.
- Produces: a working dashboard shell with sidebar navigation; agent list with a create form; agent detail with editable name, status, model, temperature, and config tone/topK; placeholder pages for every later-phase route so navigation is complete.

- [ ] **Step 1: Write the GraphQL documents**

Create `apps/web/src/graphql/operations.graphql`:

```graphql
query Me {
  me { userId email fullName organizationId organizationName role }
}

query Agents {
  agents { id name slug status model provider createdAt }
}

query Agent($id: UUID!) {
  agent(id: $id) {
    id name slug status provider model temperature maxTokens
    config { id tone language persona greeting fallbackMessage retrievalTopK maxAgentSteps }
  }
}

mutation CreateAgent($name: String!) {
  createAgent(input: { name: $name }) { id name slug status }
}

mutation UpdateAgent($id: UUID!, $input: UpdateAgentInput!) {
  updateAgent(id: $id, input: $input) {
    id name slug status provider model temperature maxTokens
  }
}

mutation UpdateAgentConfig($agentId: UUID!, $input: UpdateAgentConfigInput!) {
  updateAgentConfig(agentId: $agentId, input: $input) {
    id tone retrievalTopK maxAgentSteps
  }
}

mutation DeleteAgent($id: UUID!) {
  deleteAgent(id: $id)
}
```

Run: `cd apps/web && npm run codegen`
Expected: `src/graphql/generated.ts` contains `AgentsQuery` and `CreateAgentMutationVariables`.

- [ ] **Step 2: Write the dashboard shell**

Create `apps/web/src/app/dashboard/layout.tsx`:

```tsx
"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useAuth } from "@/lib/auth";

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/dashboard/agents", label: "Agents" },
  { href: "/dashboard/knowledge", label: "Knowledge" },
  { href: "/dashboard/products", label: "Products" },
  { href: "/dashboard/leads", label: "Leads" },
  { href: "/dashboard/prompts", label: "Prompts" },
  { href: "/dashboard/playground", label: "Playground" },
];

export default function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { user, loading, logout } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading) return <p className="p-8 text-slate-500">Loading…</p>;
  if (!user) return null;

  return (
    <div className="flex min-h-screen">
      <aside className="w-60 shrink-0 border-r border-slate-200 bg-white p-4">
        <p className="px-3 text-sm font-semibold">{user.organization_name}</p>
        <p className="mb-4 px-3 text-xs text-slate-500">{user.email}</p>
        <nav className="space-y-1">
          {NAV.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`block rounded-md px-3 py-2 text-sm ${
                pathname === item.href
                  ? "bg-slate-900 text-white"
                  : "text-slate-700 hover:bg-slate-100"
              }`}
            >
              {item.label}
            </Link>
          ))}
        </nav>
        <button
          onClick={async () => {
            await logout();
            router.replace("/login");
          }}
          className="mt-6 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
        >
          Sign out
        </button>
      </aside>
      <main className="flex-1 p-8">{children}</main>
    </div>
  );
}
```

Create `apps/web/src/components/ComingSoon.tsx`:

```tsx
export function ComingSoon({ title, phase }: { title: string; phase: string }) {
  return (
    <section>
      <h1 className="text-2xl font-semibold">{title}</h1>
      <p className="mt-2 max-w-prose text-slate-600">
        Arriving in {phase}. The navigation is in place now so the shape of the
        product is visible while the backend catches up.
      </p>
    </section>
  );
}
```

Create the five placeholder pages, each a one-liner — for example `apps/web/src/app/dashboard/knowledge/page.tsx`:

```tsx
import { ComingSoon } from "@/components/ComingSoon";

export default function KnowledgePage() {
  return <ComingSoon title="Knowledge" phase="Phase 3 (RAG)" />;
}
```

Use: Knowledge → "Phase 3 (RAG)"; Products → "Phase 4 (Agent + Tools)"; Leads → "Phase 4 (Agent + Tools)"; Prompts → "Phase 2 (Prompt management UI)"; Playground → "Phase 2 (Streaming chat)".

- [ ] **Step 3: Write the overview page**

Create `apps/web/src/app/dashboard/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useQuery } from "urql";
import { AgentsDocument } from "@/graphql/generated";

export default function DashboardPage() {
  const [{ data, fetching }] = useQuery({ query: AgentsDocument });
  const agents = data?.agents ?? [];

  return (
    <section className="space-y-6">
      <h1 className="text-2xl font-semibold">Overview</h1>
      <div className="rounded-xl border border-slate-200 bg-white p-6">
        <p className="text-sm text-slate-500">Agents</p>
        <p className="text-3xl font-semibold">{fetching ? "—" : agents.length}</p>
        <Link href="/dashboard/agents" className="mt-4 inline-block text-sm underline">
          Manage agents
        </Link>
      </div>
    </section>
  );
}
```

> `AgentsDocument` requires the `client-preset` style output. If codegen emits only types, add `@graphql-codegen/typed-document-node` to `codegen.ts` plugins and re-run `npm run codegen`.

- [ ] **Step 4: Write the agents list page**

Create `apps/web/src/app/dashboard/agents/page.tsx`: a client component that runs the `Agents` query, renders a table of name / slug / status / model with each row linking to `/dashboard/agents/[id]`, and a form with a single name input that calls the `CreateAgent` mutation and refetches on success. Render mutation errors from `result.error.graphQLErrors[0].message` in a `role="alert"` paragraph. Show "No agents yet. Create your first one above." when the list is empty.

- [ ] **Step 5: Write the agent detail page**

Create `apps/web/src/app/dashboard/agents/[id]/page.tsx`: reads `params.id`, runs the `Agent` query, and renders two forms —

1. **Agent** — name (text), status (select: DRAFT / ACTIVE / DISABLED), provider (select: openai / anthropic), model (text), temperature (number, step 0.1, 0–2), max tokens (number). Submits `UpdateAgent`.
2. **Behaviour** — tone (text), retrieval top-K (number, 1–50), max agent steps (number, 1–20). Submits `UpdateAgentConfig` with `agentId`.

Each form shows a transient "Saved" confirmation on success and an error paragraph on failure. Include a "Delete agent" button that calls `DeleteAgent` after a `window.confirm` and routes back to `/dashboard/agents`.

- [ ] **Step 6: Verify**

Run: `cd apps/web && npm run typecheck && npm run lint && npm run build`
Expected: clean build.

Then manually: sign in as `demo@example.com` / `demo-password-123`, confirm "Demo Sales Agent" is listed, create a second agent, open it, change the temperature, save, reload, and confirm the value persisted.

- [ ] **Step 7: Commit**

```bash
git add apps/web
git commit -m "feat: add dashboard shell with agent list and detail editing"
```

---

### Task 13: Full-stack compose, README, and CI

**Files:**
- Create: `apps/api/Dockerfile`, `apps/web/Dockerfile`, `apps/api/.dockerignore`, `apps/web/.dockerignore`
- Modify: `docker-compose.yml` (add `api` and `web`)
- Create: `README.md`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: everything above.
- Produces: `docker compose up` starting all four services; `http://localhost:3000` reaching a working dashboard; CI running lint, typecheck, and the full test suite.

- [ ] **Step 1: Write the Dockerfiles**

Create `apps/api/Dockerfile`:

```dockerfile
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev || uv sync --no-dev

COPY . .

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
```

Create `apps/api/.dockerignore` containing `.venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `tests`.

Create `apps/web/Dockerfile`:

```dockerfile
FROM node:22-alpine

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install

COPY . .

EXPOSE 3000
CMD ["npm", "run", "dev"]
```

Create `apps/web/.dockerignore` containing `node_modules`, `.next`.

- [ ] **Step 2: Extend docker-compose.yml**

Add to the `services` block:

```yaml
  api:
    build: ./apps/api
    depends_on:
      db: { condition: service_healthy }
      redis: { condition: service_healthy }
    environment:
      DATABASE_URL: postgresql+asyncpg://app_user:app_user_password@db:5432/saas_ai
      MIGRATION_DATABASE_URL: postgresql+asyncpg://app_owner:app_owner_password@db:5432/saas_ai
      REDIS_URL: redis://redis:6379/0
      JWT_SECRET: ${JWT_SECRET:-local-development-secret-change-me}
      ENVIRONMENT: local
      CORS_ORIGINS: http://localhost:3000
    ports:
      - "8000:8000"
    volumes:
      - ./apps/api:/app
    command: >
      sh -c "uv run alembic upgrade head &&
             uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"

  web:
    build: ./apps/web
    depends_on:
      - api
    environment:
      NEXT_PUBLIC_API_URL: http://localhost:8000
    ports:
      - "3000:3000"
    volumes:
      - ./apps/web:/app
      - /app/node_modules
```

> The API container runs migrations on start. That is right for local development and wrong for production, where migrations belong in a separate step that runs once rather than once per replica. Noted here so the distinction is deliberate.

- [ ] **Step 3: Verify the full stack from clean**

Run:
```bash
docker compose down -v
cp .env.example .env
docker compose up --build -d
sleep 30
curl -s localhost:8000/health/ready
docker compose exec -T api uv run python -m app.db.seed
curl -s localhost:3000 -o /dev/null -w "%{http_code}\n"
```
Expected: readiness reports both checks `true`; the seed prints created credentials; the web root returns 200 or 307.

- [ ] **Step 4: Write the README**

Create `README.md` covering, in this order: what the product is (three sentences); the architecture diagram from `docs/ARCHITECTURE.md` §2.1; a quick-start (`cp .env.example .env`, `docker compose up`, `make seed`, sign in at `localhost:3000` with the demo credentials); the repository layout; every `make` target with a one-line description; how to run tests and linters; the phase roadmap with Phase 1 marked complete; and a short "Design decisions" section linking to `docs/ARCHITECTURE.md` for RLS, the GraphQL/SSE split, and prompt versioning.

- [ ] **Step 5: Write CI**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push: { branches: [main] }
  pull_request:

jobs:
  api:
    runs-on: ubuntu-latest
    services:
      db:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: postgres
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: saas_ai
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U postgres" --health-interval 5s
          --health-timeout 5s --health-retries 20
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping" --health-interval 5s
          --health-timeout 5s --health-retries 20

    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: |
          PGPASSWORD=postgres psql -h localhost -U postgres -d saas_ai \
            -f infrastructure/postgres/init.sql
      - working-directory: apps/api
        env:
          DATABASE_URL: postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai
          MIGRATION_DATABASE_URL: postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai
          REDIS_URL: redis://localhost:6379/0
          JWT_SECRET: ci-secret-not-for-production
        run: |
          uv sync
          uv run ruff check .
          uv run ruff format --check .
          uv run mypy app/
          uv run alembic upgrade head
          uv run pytest -v

  web:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: "22" }
      - working-directory: apps/web
        run: |
          npm ci
          npm run lint
          npm run typecheck
```

> Codegen output is git-ignored, so `typecheck` in CI needs `src/graphql/generated.ts`. Either commit the generated file (and drop it from `.gitignore`) or add a codegen step to the web job that first exports the schema. Committing it is simpler and makes CI independent of a running API — do that, and remove the `apps/web/src/graphql/generated.ts` line from `.gitignore`.

- [ ] **Step 6: Run the full verification**

Run:
```bash
make lint && make test
cd apps/web && npm run typecheck && npm run lint && npm run build
```
Expected: everything passes.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat: add full-stack docker compose, README, and CI"
```

---

## Phase 1 exit criteria

Verify each before declaring Phase 1 complete. These match §9.5 of the spec.

- [ ] `docker compose down -v && cp .env.example .env && docker compose up` reaches a working app with no further manual steps.
- [ ] Register → login → create agent → edit agent → reload works in the browser.
- [ ] `make test` is green; `make lint` is clean; `npm run typecheck` and `npm run build` are clean.
- [ ] `tests/integration/test_tenant_isolation.py` and `tests/integration/test_rls.py` both pass in full.
- [ ] `README.md` documents setup, architecture, and every command.

---

## Plan self-review

**Spec coverage.** Walking §9 of `docs/ARCHITECTURE.md`:

| Spec requirement | Task |
|---|---|
| §9.1 compose, init.sql, .env.example, Makefile | 1, 13 |
| §9.2 core (config, logging, errors, security, tenancy) | 2, 4, 5 |
| §9.2 DB models + repositories | 3, 4, 7, 8 |
| §9.2 Alembic with RLS + partial unique index | 3, 4, 7, 8 |
| §9.2 auth endpoints incl. refresh rotation | 6 |
| §9.2 GraphQL queries and all seven mutations | 9 |
| §9.2 middleware: request id, logging, CORS, rate limiting | 2, 6 |
| §9.2 health + readiness | 2, 3, 6 |
| §9.2 seed | 10 |
| §9.3 Next.js, urql, codegen, auth pages, stubs | 11, 12 |
| §9.4 unit, integration, isolation suite | 2, 3, 5, 6, 7, 8, 10 |
| §9.5 definition of done | Exit criteria |
| §2.3 two-layer tenancy incl. RLS | 4 (policy), 4 (repository base) |
| §3.1–3.2 schema for orgs/users/memberships/agents/configs/prompts/versions | 3, 7, 8 |

Two gaps found and closed while reviewing: the `agents.prompt_id` foreign key had no home (it was created before `prompts` existed) — it is now added in Task 8's migration with the column created nullable in Task 7; and the refresh cookie's `path` was scoped to `/api/v1/auth`, which the Next.js middleware in Task 11 cannot read — Task 11 Step 7 changes it to `/` and re-runs the auth tests.

A third was closed on a second pass: the GraphQL context originally raised on a missing token, which FastAPI's `AppError` handler would have turned into a bare 401 JSON body rather than the GraphQL error shape `test_graphql_requires_authentication` asserts. Task 9 now builds a tenant-less context and checks authentication in `_require_tenant` at the top of each resolver.

**Type consistency.** `AgentService` methods used in Task 9 (`list_agents`, `get_agent`, `get_config`, `create_agent`, `update_agent`, `update_config`, `delete_agent`) match Task 7's definitions. `PromptService` methods used in Task 9 and Task 10 (`list_prompts`, `get_prompt`, `create_prompt`, `create_version`, `activate_version`, `active_version`) match Task 8. `TenantContext` field names are identical in Tasks 4, 6, 7, 8, and 10. `tenant_from_bearer` is defined in Task 6 and consumed in Task 9. `DEFAULT_SALES_SYSTEM_PROMPT` is defined in Task 8 and consumed in Task 10.

**Placeholders.** Tasks 12 Steps 4 and 5 describe two React pages in prose rather than full code — a deliberate exception, since they are mechanical forms over operations whose exact shape is fully specified in Step 1's GraphQL documents and whose field lists, validation bounds, and error-rendering approach are given explicitly. Every backend step contains runnable code.


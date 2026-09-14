.PHONY: up down logs verify-db

up:
	docker compose up -d db redis

down:
	docker compose down

logs:
	docker compose logs -f

verify-db:
	bash infrastructure/scripts/verify_db.sh

.PHONY: api test lint

api:
	cd apps/api && uv run uvicorn app.main:app --reload --port 8000

test:
	cd apps/api && uv run pytest -v

lint:
	cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app/

.PHONY: migrate revision

migrate:
	cd apps/api && uv run alembic upgrade head

revision:
	cd apps/api && uv run alembic revision -m "$(m)"

.PHONY: schema

schema:
	cd apps/api && uv run strawberry export-schema app.graphql.schema:schema > ../../packages/shared/schema.graphql

.PHONY: seed

seed:
	cd apps/api && uv run python -m app.db.seed

.PHONY: up down logs verify-db

up:
	docker compose up -d db redis

down:
	docker compose down

logs:
	docker compose logs -f

verify-db:
	bash infrastructure/scripts/verify_db.sh

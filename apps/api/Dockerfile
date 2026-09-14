# Production image for the API (Render). docker-compose overrides the command
# for local development; nothing here assumes --reload or a mounted source tree.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PORT=8000

# Dependencies resolve in their own layer so application edits do not
# reinstall the world. --locked fails loudly if uv.lock is stale.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

EXPOSE 8000
# Render injects $PORT; the default above keeps `docker run -p 8000:8000` working.
CMD ["sh", "-c", "uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

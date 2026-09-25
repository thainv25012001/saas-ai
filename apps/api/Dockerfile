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
# --no-access-log: uvicorn's own access log line prints the client IP and the
# full request path -- including a widget public key in full, unlike
# app/main.py's own request-logging middleware, which already logs every
# request and truncates a widget key to its first 8 characters. Spec §5 says
# no IP in logs; uvicorn's line would otherwise be a second, unredacted copy
# of what the middleware already logs correctly. See docs/DEPLOYMENT.md.
# --proxy-headers/--forwarded-allow-ips: the per-IP rate limits key on the
# client address (app/core/request.py), which behind a proxy is the proxy's
# unless X-Forwarded-For is honoured -- but only from a trusted hop, or anyone
# could forge a fresh key per request. Loopback by default; render.yaml sets
# FORWARDED_ALLOW_IPS='*' because Render's proxy is the only way in.
CMD ["sh", "-c", "uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-access-log --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]

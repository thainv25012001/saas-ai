import asyncio
from typing import Any

from fastapi import APIRouter, Request

from app.core.errors import NotFoundError

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
async def health() -> dict[str, str]:
    """Liveness: the process is up. No dependencies are touched."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    """Readiness: dependencies are reachable."""
    from app.core import redis as redis_module
    from app.db import session as session_module

    # Concurrently: the two checks share nothing, and this path is polled every
    # few seconds by the compose healthcheck, whose own timeout their sum has to
    # fit inside. Both swallow their exceptions and answer with a bool, so there
    # is nothing here for `gather` to re-raise.
    database_ok, redis_ok = await asyncio.gather(
        session_module.check_database(),
        redis_module.check_redis(),
    )
    dependencies_ok = database_ok and redis_ok
    # The access-log middleware drops the line for a probe that passed (see
    # `_probe_passed` in app.main). It only sees the status code, and a
    # degraded readiness still answers 200 — the body is the verdict — so the
    # verdict has to be handed back up through the shared request scope.
    request.state.probe_failed = not dependencies_ok
    return {
        "status": "ready" if dependencies_ok else "degraded",
        "checks": {"database": database_ok, "redis": redis_ok},
    }


@router.get("/boom")
async def boom() -> dict[str, str]:
    """Exercises the AppError handler. Kept deliberately — it is the only
    end-to-end assertion that the error envelope is wired up."""
    raise NotFoundError("deliberate test failure")

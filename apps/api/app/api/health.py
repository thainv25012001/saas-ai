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
    """Readiness: dependencies are reachable."""
    from app.core.redis import check_redis
    from app.db.session import check_database

    database_ok = await check_database()
    redis_ok = await check_redis()
    return {
        "status": "ready" if database_ok and redis_ok else "degraded",
        "checks": {"database": database_ok, "redis": redis_ok},
    }


@router.get("/boom")
async def boom() -> dict[str, str]:
    """Exercises the AppError handler. Kept deliberately — it is the only
    end-to-end assertion that the error envelope is wired up."""
    raise NotFoundError("deliberate test failure")

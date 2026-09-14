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

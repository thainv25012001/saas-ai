from functools import lru_cache
from typing import cast

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@lru_cache
def get_redis() -> Redis:
    # `Redis.from_url` carries no return annotation in redis-py 5.x (the
    # version arq's `redis<6` pin forces this project onto), so mypy sees
    # its result as `Any` rather than `Redis`. The cast states what is
    # actually true at runtime, not a workaround for one.
    return cast(Redis, Redis.from_url(get_settings().redis_url, decode_responses=True))


async def check_redis() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        # /health/ready is public, so it answers with a bare boolean. The
        # reason goes to the log, where the operator can actually read it:
        # without this, a REST URL, an unset variable and a firewalled host
        # are three different bugs wearing the same `redis: false`.
        logger.warning("redis_unreachable", error=f"{type(exc).__name__}: {exc}")
        return False

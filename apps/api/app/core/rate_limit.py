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

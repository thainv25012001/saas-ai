from collections.abc import Awaitable
from typing import cast

from redis.exceptions import RedisError

from app.core.errors import RateLimitError
from app.core.logging import get_logger
from app.core.redis import get_redis

logger = get_logger(__name__)

# INCR and EXPIRE as a single script, not two round trips: if a connection
# blip landed between two separate commands, a key could be left with a
# count but no TTL. It would then grow forever (later calls see count != 1
# and never retry the EXPIRE), permanently locking out that key with no
# self-healing path. A Lua script runs atomically on the Redis server, so
# there is no window in which that partial state can be observed.
_INCR_AND_EXPIRE = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def enforce_rate_limit(key: str, *, limit: int, window_seconds: int) -> None:
    """Fixed-window counter in Redis.

    Chosen over a sliding window because it is one script and the failure
    mode (up to 2x the limit across a window boundary) is irrelevant for
    login throttling.
    """
    redis = get_redis()
    redis_key = f"ratelimit:{key}"
    try:
        # redis-py 5.x (arq's `redis<6` pin forces this project onto it)
        # types `eval`'s ARGV varargs as `str` and its return as
        # `Awaitable[str] | str` -- a sync/async-shared stub that does not
        # narrow for the async client actually in use here, and does not
        # match what it actually returns: the Lua script's `return count`
        # comes back as a Python `int`, confirmed live against a running
        # Redis. Both casts state what is true at runtime -- Lua ARGV
        # values are always strings on the wire regardless of what Python
        # type produced them, and this client's `eval` always returns an
        # awaitable of that `int` -- not what the stub happens to claim.
        raw = await cast(
            Awaitable[int],
            redis.eval(_INCR_AND_EXPIRE, 1, redis_key, str(window_seconds)),
        )
    except RedisError:
        # Deliberate trade, not an oversight: fail OPEN. An auth endpoint
        # that is entirely unavailable because its rate limiter's backing
        # store blipped is a worse outage than briefly-unthrottled auth -
        # passwords are still Argon2-verified regardless of this branch.
        #
        # Since Phase 4 this is no longer only an auth argument: the same
        # branch also un-throttles `create_lead` (`app/tools/leads.py`), the
        # one tool in the system that writes, callable by an anonymous chat
        # widget visitor. The trade still holds, but for a different reason
        # there: `POST /chat/stream` carries its own per-user limit, a lead
        # row is inert until a human acts on it, and -- since the
        # whole-branch review's Critical 1 fix -- `create_lead` runs at all
        # only for an agent an operator explicitly granted it to. A Redis
        # outage therefore widens the window on a tool most agents cannot
        # call, rather than on every conversation in the deployment.
        # Only genuine Redis/connection errors land here; RateLimitError
        # itself is raised below, outside this try block, and must never
        # be caught by it.
        # Only the key's leading scope ("login", "widget", ...), never the
        # whole key: its subject is an IP or a widget visitor id, neither of
        # which may reach a log line (widget spec §5). An outage fails every
        # key alike, so the full key would add nothing to diagnose it.
        logger.warning("rate_limit_backend_unavailable", key_scope=key.split(":", 1)[0])
        return
    if raw > limit:
        raise RateLimitError("too many requests, please try again shortly")

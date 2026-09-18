"""Putting a job on arq's Redis queue.

One place, because the three things that are easy to get wrong here are the
same for every job: the Redis DSN shape, naming the job by the function's own
`__name__`, and closing the pool with `aclose()`. Two copies of that was two
places to keep in sync and a third job would have been a third; each caller is
now a typed wrapper naming its own arguments.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from arq import create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings


async def enqueue(task: Callable[..., Awaitable[Any]], /, **kwargs: str) -> None:
    """Queue `task`, passing `kwargs` to it as keyword arguments.

    Opens and closes its own connection pool per call rather than holding a
    long-lived one: enqueuing happens once per upload and once per
    conversation, so pool reuse would add a lifecycle to manage for no
    measurable benefit.
    """
    pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    try:
        # The function object's own `__name__`, never a literal: arq derives a
        # job's registered name from it (see `WorkerSettings.functions`), and a
        # copy would drift the moment the function is renamed. The failure is
        # invisible -- jobs pile up in Redis with no worker listening for them.
        # The cast is for arq's signature, not for correctness: `enqueue_job`
        # declares reserved keyword-only parameters (`_job_id`, `_defer_by`,
        # ...) beside its `**kwargs`, so mypy has to assume a `dict[str, str]`
        # splat might be binding to one of those and rejects it on their
        # types. Every key here is a job argument.
        await pool.enqueue_job(task.__name__, **cast(dict[str, Any], kwargs))
    finally:
        # `aclose()`, not the deprecated `close()`: redis-py 5.0.1+ emits a
        # `DeprecationWarning` from `close()` (see `redis/utils.py`'s
        # `deprecated_function` wrapper), invisible until something actually
        # calls this for real -- and this suite's `filterwarnings = ["error"]`
        # (pyproject.toml) turns any warning into a hard failure.
        await pool.aclose()

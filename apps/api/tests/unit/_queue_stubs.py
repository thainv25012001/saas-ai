"""A fake arq pool, shared by the enqueue tests.

Here for the same reason `_llm_stubs.py` is: two test modules needed the same
stub, and two copies of it encode arq's pool contract in two places that must
then be changed together -- where only one of them would fail.
"""

from typing import Any


class FakePool:
    def __init__(self) -> None:
        self.enqueued: dict[str, Any] = {}
        self.closed = False

    async def enqueue_job(self, name: str, **kwargs: Any) -> None:
        self.enqueued = {"name": name, "kwargs": kwargs}

    async def aclose(self) -> None:
        self.closed = True

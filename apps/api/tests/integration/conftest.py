from collections.abc import AsyncIterator

import pytest


@pytest.fixture(autouse=True)
async def _dispose_shared_engine() -> AsyncIterator[None]:
    """anyio's strict mode tears down and rebuilds the event loop between
    every test function, but app.db.session.engine is a module-level
    singleton whose connection pool would otherwise survive across that
    boundary. A pooled asyncpg connection opened under one test's loop
    cannot be closed under a later test's loop — it raises deep inside
    asyncio's proactor transport during pool cleanup. Disposing the pool
    at the end of every test, while its own loop is still alive, keeps each
    test's connections scoped to that test's loop.

    Scoped to tests/integration only: unit tests are plain synchronous
    functions with no event loop, and this fixture requires one.
    """
    yield
    from app.db.session import engine

    await engine.dispose()

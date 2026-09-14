import os
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import pytest
from httpx import ASGITransport, AsyncClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

# Set before importing the app: Settings reads the environment at import time.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://app_user:app_user_password@localhost:5432/saas_ai",
)
os.environ.setdefault(
    "MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://app_owner:app_owner_password@localhost:5432/saas_ai",
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-for-production")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def owner_connection() -> AsyncIterator["AsyncConnection"]:
    """A connection as app_owner — bypasses RLS. Use it to assert on schema
    and to set up fixture data across organizations."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    engine = create_async_engine(get_settings().migration_database_url)
    async with engine.connect() as connection:
        yield connection
    await engine.dispose()


@pytest.fixture
async def clean_users(owner_connection: "AsyncConnection") -> AsyncIterator[None]:
    """Auth tests share a database. Remove test rows before and after so each
    test starts from a known state, and rate-limit counters do not bleed."""
    from sqlalchemy import text

    async def _purge() -> None:
        await owner_connection.execute(text("DELETE FROM users WHERE email LIKE '%@example.com'"))
        await owner_connection.execute(
            text("DELETE FROM organizations WHERE slug LIKE 'ada-motors%'")
        )
        await owner_connection.commit()

    await _purge()
    await _flush_rate_limits()
    yield
    await _purge()


async def _flush_rate_limits() -> None:
    from app.core.redis import get_redis

    redis = get_redis()
    keys = [key async for key in redis.scan_iter("ratelimit:*")]
    if keys:
        await redis.delete(*keys)

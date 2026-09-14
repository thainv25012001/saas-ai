import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

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

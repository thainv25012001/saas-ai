from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url


def _find_env_file(start: Path | None = None) -> Path | None:
    """Locate .env relative to this module, not the working directory.

    Migrations and the app are launched from different directories; a cwd-relative
    env_file silently finds nothing from apps/api/. Returns None inside containers,
    where configuration arrives as real environment variables instead.

    `start` defaults to this file's own location and exists only so tests can point
    the walk at an isolated directory tree instead of the real repo.
    """
    for parent in (start if start is not None else Path(__file__)).resolve().parents:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def _normalize_database_url(value: str) -> str:
    """Accept the connection URL a managed Postgres hands you, unchanged.

    Neon, Render, Supabase and Heroku all emit libpq-shaped URLs, and every
    one of them breaks this app in a way that names something else:

    * no driver in the scheme (`postgresql://`, or the legacy `postgres://`)
      makes SQLAlchemy load its default DBAPI, psycopg2, which this project
      does not depend on - so a production boot dies at import time with
      `ModuleNotFoundError: No module named 'psycopg2'`, which says nothing
      about the actual mistake;
    * `sslmode` and `channel_binding` are libpq parameter names. The asyncpg
      dialect forwards unknown query parameters straight to
      `asyncpg.connect()`, which has no such keyword arguments, so the first
      query fails with a TypeError long after startup looked healthy.

    Only the driver-less schemes are rewritten. Naming a driver explicitly is
    a deliberate choice and is left alone.
    """
    url = make_url(value)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+asyncpg")
    if url.drivername != "postgresql+asyncpg":
        return url.render_as_string(hide_password=False)

    query = dict(url.query)
    sslmode = query.pop("sslmode", None)
    # asyncpg has no channel binding parameter. TLS itself is preserved below;
    # only the SCRAM channel-binding negotiation, which asyncpg cannot be told
    # to require, is dropped.
    query.pop("channel_binding", None)
    if sslmode is not None and "ssl" not in query:
        query["ssl"] = sslmode
    # hide_password=False: the default masks the password as ***, which would
    # turn this helper into an authentication failure.
    return url.set(query=query).render_as_string(hide_password=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_find_env_file(), env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str
    migration_database_url: str
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    environment: str = "local"
    log_level: str = "INFO"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # `fake` keeps the playground working with no key configured. Set to
    # `openai` or `anthropic` once a key is present.
    default_llm_provider: str = "fake"

    @field_validator("database_url", "migration_database_url", mode="after")
    @classmethod
    def normalize_postgres_dsn(cls, value: str) -> str:
        return _normalize_database_url(value)

    @field_validator("redis_url", mode="after")
    @classmethod
    def reject_non_tcp_redis_url(cls, value: str) -> str:
        """Upstash presents a REST endpoint (https://) next to the TCP one,
        and the REST URL is the easier of the two to copy. redis-py speaks
        only the Redis protocol: handed an https:// URL it raises inside
        check_redis(), which catches everything, so the only symptom is
        `/health/ready` reporting `redis: false` forever with no reason
        anywhere. Fail at startup instead, naming what to paste."""
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError(
                "REDIS_URL must be a Redis protocol URL (redis://, rediss:// or unix://), "
                f"not {value.split('://')[0]}://. Upstash calls this the TCP endpoint and it "
                "looks like rediss://default:<password>@<host>:6379 - the https:// URL on the "
                "same page is the REST API, which this client cannot speak."
            )
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def split_comma_separated(cls, value: object) -> object:
        """CORS_ORIGINS is a comma-separated string in .env, a list in code."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment

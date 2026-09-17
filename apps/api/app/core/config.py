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
    openrouter_api_key: str | None = None
    # `fake` keeps the playground working with no key configured. Set to
    # `openai` or `anthropic` once a key is present.
    default_llm_provider: str = "fake"
    # `hashing` keeps ingestion and retrieval working with no key configured.
    # Set to `openai` once a key is present.
    embedding_provider: str = "hashing"

    # Where uploaded document bytes live between the upload request and the
    # worker picking them up. A local directory, not object storage -- see
    # the module docstring on `app/rag/storage.py` for why that is fine for
    # a single instance and wrong for production.
    upload_dir: str = "./var/uploads"
    # Ceiling on the whole `POST /api/v1/documents` request body -- not
    # just the file part -- enforced by app/api/documents.py before it
    # ever writes any bytes anywhere. Named for what is actually measured
    # (the request, including multipart boundaries/headers and the `title`
    # field) rather than `max_upload_bytes`, which this used to be called
    # until a review caught the two saying different things: the intended
    # meaning ("one uploaded document's size") does not match a cap that is
    # actually compared against Content-Length. 20 MB comfortably covers a
    # policy PDF or a product manual, with headroom to spare for that
    # overhead, while keeping a single upload's embedding cost -- and how
    # long a worker holds a job -- bounded.
    max_request_bytes: int = 20 * 1024 * 1024
    # Chunks per embedding API call. Higher batches ingest faster but put
    # more chunks at risk of a single request failing; 64 is comfortably
    # under every provider's per-request item limit we target.
    embedding_batch_size: int = 64
    # Attempts per batch before the whole ingest job is failed rather than
    # storing whatever batches happened to succeed.
    embedding_max_retries: int = 3
    # Base delay between retries of the same batch; multiplied by the
    # attempt number for simple linear backoff. Small default so a failing
    # embedding call does not make an already-failing test slow too.
    embedding_retry_backoff_seconds: float = 0.1
    # How many ingest jobs `app.workers.settings.WorkerSettings` runs at
    # once. `ingest_document(app/rag/ingest.py)` holds up to two of this
    # worker process's own connections at a time -- the caller's `session`
    # plus a second, independent `tenant_session` for `mark_processing` or
    # (on failure) `mark_failed` -- never three, since `mark_processing`'s
    # transaction always closes before the failure path could open a third.
    # `app/db/session.py`'s engine gives this process `pool_size=10 +
    # max_overflow=5 = 15` connections total, so `2 * worker_max_jobs` must
    # stay comfortably under that; 5 (10 connections at saturation) leaves
    # headroom rather than running right up against 7 (the exact floor).
    # Change this alongside `pool_size`/`max_overflow` if either moves.
    worker_max_jobs: int = 5

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

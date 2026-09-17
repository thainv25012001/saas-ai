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
    # The relevance floor on the *vector* arm of hybrid retrieval, as a
    # pgvector cosine distance (`<=>`, 0 = identical, 1 = orthogonal). A
    # candidate further away than this is dropped before fusion, because
    # Reciprocal Rank Fusion cannot express relevance at all -- its scores
    # come from rank position, so the top hit is exactly `1/61` whatever it
    # contains. Without a floor here, the vector retriever returned its
    # nearest rows unconditionally and *every* turn, `hi` included, cited up
    # to `top_k` chunks as having grounded the answer.
    #
    # 0.8 was chosen by measuring `HashingEmbedder` against small multi-topic
    # corpora (see tests/integration/test_retrieve.py). **The two classes it
    # divides are close together and do overlap** -- this is a useful
    # threshold, not a clean separation, and anyone tuning it should start
    # from that. Across 33 queries over two handbooks:
    #
    #   questions the corpus answers   0.556 - 0.860
    #   conversational / off-topic     0.792 - 1.000
    #
    # so the band around 0.79-0.86 contains both. Real examples on either
    # side of the line: "How often do I need an oil change?" measures 0.8595
    # and is dropped by the vector arm (the keyword arm still answers it),
    # while "can you write me a poem about the sea" measures 0.7918 and is
    # kept. An earlier version of this comment claimed 0.60-0.71 against
    # 0.82-1.00; that range was back-fitted to the eight queries it was
    # derived from and did not survive a wider sample.
    #
    # What makes 0.8 worth keeping anyway is the direction of its mistakes,
    # measured rather than assumed: of 14 relevant queries 13 still retrieve,
    # and the one dropped had been citing the *wrong* section, so the floor
    # turned a wrong citation into no citation; 10 of 11 filler queries now
    # retrieve nothing where every one of them previously cited up to
    # `top_k`. The two arms also cover for each other -- a question the
    # vector arm drops is usually one the keyword arm matches lexically.
    #
    # The classes sit this close because `HashingEmbedder` is a hashed
    # bag-of-words that keeps stopwords, so shared function words alone push
    # an unrelated query to about 0.79.
    #
    # This number is a property of the embedding model and the corpus, not of
    # the application, which is why it is a setting and not a constant. A
    # real semantic embedder puts *unrelated* text around 0.2-0.3, where 0.8
    # admits nearly everything: the failure mode of a badly-fitted value here
    # is permissive (back to citing irrelevant chunks), not silent (a corpus
    # that never answers) -- the safer direction, but not a reason to skip
    # re-measuring on any `EMBEDDING_PROVIDER` change.
    retrieval_max_cosine_distance: float = 0.8
    # The relevance floor on the keyword arm's *fallback* form only -- see
    # `app/rag/retrieve.py` for why that arm has two forms. The strict form
    # requires every content word of the query to be present, which is a
    # relevance predicate in itself and needs no threshold. The fallback
    # requires only one, so without a floor a question that happens to share
    # a single stemmed word with the corpus ("does it come in red?" against a
    # warranty chunk that says "whichever comes first") would cite it.
    #
    # `ts_rank_cd` with the default normalization returns roughly 0.1 per
    # matched lexeme occurrence in the cover, so 0.15 reads as "more than one
    # incidental word matched". Measured on the same corpora as
    # `retrieval_max_cosine_distance`: real questions score 0.2-0.5 against
    # the section that answers them, single-word coincidences score 0.1.
    #
    # This one is load-bearing for *correctness*, not just quality, and
    # setting it to 0 is not merely "less filtering". `websearch_to_tsquery`
    # turns a leading hyphen into negation, so a query combining a negated
    # term with a term the corpus lacks -- "-cat dog" -> `!'cat' & 'dog'` --
    # matches nothing in the strict form, falls back to the OR form
    # `!'cat' | 'dog'`, and *that* matches every chunk not containing "cat":
    # the entire corpus. `ts_rank_cd` scores a negated match 0.0, so this
    # floor is the only thing standing between such a query and citing
    # everything. Measured, not inferred.
    #
    # Known limit, not covered by this floor: a *bare* negation ("-cat",
    # "-warranty") matches the whole corpus through the **strict** form,
    # which has no rank floor at all, so it still returns up to `top_k`
    # arbitrary chunks. The fix belongs on the strict arm, not here.
    # No test pins either case yet.
    retrieval_min_keyword_rank: float = 0.15

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

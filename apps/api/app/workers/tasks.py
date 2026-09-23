"""arq job functions. This module's function names are arq's job names --
`WorkerSettings.functions` registers them by identity (or, for a job that
needs its own timeout, through `arq.worker.func`, which keeps the same
name), and `enqueue_ingest` enqueues them by the matching string, so renaming a function here is a
breaking change to anything already queued under the old name.
"""

import time
import uuid
from typing import Any

from app.agents.service import AgentService
from app.conversations.service import ConversationService
from app.conversations.titles import (
    TITLE_MAX_TOKENS,
    TITLE_SYSTEM_PROMPT,
    build_title_messages,
    clean_title,
    fallback_title,
)
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import MessageRole
from app.documents.service import DocumentService
from app.evaluations.runner import run_evaluation
from app.llm.errors import LLMError
from app.llm.registry import get_provider
from app.llm.types import CompletionRequest
from app.products.importer import ProductImportService, load_import_bytes, run_import
from app.rag.ingest import bounded_error_message, ingest_document
from app.rag.storage import load_document_bytes

logger = get_logger(__name__)

#: arq's `max_tries` for `run_evaluation_task`, registered with it in
#: `WorkerSettings.functions` and passed to `run_evaluation` -- which must
#: know when a try is the last one (see its time budget).
EVALUATION_MAX_TRIES = 3


async def ingest_document_task(
    ctx: dict[str, Any], *, organization_id: str, document_id: str
) -> None:
    """The arq entry point for document ingestion.

    Runs in a background worker process, not inside an HTTP request, so
    unlike `ingest_document` (which takes an already-open session) this
    function has to open its own tenant-scoped transaction end to end --
    there is no request here to have opened one for it. `ctx` is arq's
    per-job context (its Redis pool, job id, retry count, ...); this job
    does not need anything out of it, but arq always calls a job function
    with it as the first positional argument.

    The document is looked up under `organization_id`'s RLS *before*
    anything is read from disk. That ordering matters: a job enqueued (or
    tampered with) against the wrong organization for this `document_id`
    finds no row here and fails right here with `NotFoundError`, rather than
    reading another tenant's bytes off disk and ingesting them under the
    wrong organization's chunks.
    """
    tenant = TenantContext(
        organization_id=uuid.UUID(organization_id),
        user_id=None,
        role=None,
        request_id=f"ingest:{document_id}",
    )
    document_uuid = uuid.UUID(document_id)
    # `IngestResult` used to be constructed and dropped on the floor here.
    # These three log lines are the only signal an operator gets that a
    # document was ever processed at all -- `documents.error` says what went
    # wrong for the one document that failed, and nothing anywhere said how
    # long a queue of them took or how much corpus came out.
    #
    # Deliberately absent: the document's text, its title and its
    # embeddings. The whole point of the bounded `documents.error` message
    # (see `_error_message` in app/rag/ingest.py) is undone if the same
    # content goes to the log instead.
    logger.info("ingest_started", document_id=document_id, organization_id=organization_id)
    started_at = time.monotonic()
    try:
        async with tenant_session(tenant) as session:
            document = await DocumentService(session, tenant).get(document_uuid)
            # `mime_type` is nullable on the row (see
            # app/db/models/document.py); falling back to "" rather than
            # raising here lets `extract()` reject it the same way it
            # rejects any other unsupported type, going through the
            # ordinary failed-status path instead of a second one.
            data = load_document_bytes(tenant.organization_id, document_uuid)
            result = await ingest_document(
                session, tenant, document_uuid, data, document.mime_type or ""
            )
    except Exception as exc:
        logger.error(
            "ingest_failed",
            document_id=document_id,
            organization_id=organization_id,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            # `bounded_error_message`, and deliberately no `exc_info`: a
            # rendered traceback ends with the exception's own `str()`, and
            # for a SQLAlchemy DBAPIError that is the statement *and its
            # bound parameters* -- the document's text and its embedding
            # floats, straight into the log. Exactly what recording a
            # bounded message in `documents.error` exists to prevent (see
            # `bounded_error_message`), undone by logging it instead.
            error=bounded_error_message(exc),
        )
        raise
    logger.info(
        "ingest_completed",
        document_id=document_id,
        organization_id=organization_id,
        chunk_count=result.chunk_count,
        token_count=result.token_count,
        embedding_model=result.embedding_model,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )


async def title_conversation_task(
    ctx: dict[str, Any], *, organization_id: str, conversation_id: str
) -> None:
    """Give a conversation a short, readable title.

    Runs in the background, never on the chat request's critical path: an LLM
    call there would sit inside the turn's own transaction, on a connection
    the streaming body still owns, and a slow provider would delay the answer
    the user actually asked for.

    Like `ingest_document_task` above, this opens its own tenant-scoped
    transaction and looks the conversation up under `organization_id`'s RLS
    first, so a job enqueued against the wrong organization finds nothing
    rather than retitling another tenant's conversation.
    """
    tenant = TenantContext(
        organization_id=uuid.UUID(organization_id),
        user_id=None,
        role=None,
        request_id=f"title:{conversation_id}",
    )
    conversation_uuid = uuid.UUID(conversation_id)

    async with tenant_session(tenant) as session:
        conversations = ConversationService(session, tenant)
        conversation = await conversations.get(conversation_uuid)

        # The whole cost control, in one condition. It is what makes this one
        # call per conversation rather than one per turn, what makes a retry
        # free, and -- because the fallback below is a real write -- what
        # stops a conversation whose provider is broken being re-billed
        # forever.
        if conversation.title is not None:
            return

        messages = await conversations.history(conversation_uuid)
        first_user = next((m for m in messages if m.role is MessageRole.USER), None)
        if first_user is None:
            # Enqueued after the turn commits, but a lost race must be a
            # retry, not a title invented from an empty conversation.
            logger.info("conversation_title_skipped_no_messages", conversation_id=conversation_id)
            return
        user_text = first_user.content or ""
        assistant_text = next(
            (m.content or "" for m in messages if m.role is MessageRole.ASSISTANT), ""
        )

        agent = await AgentService(session, tenant).get_agent(conversation.agent_id)
        title: str | None = None
        try:
            provider = get_provider(agent.provider)
            response = await provider.generate(
                CompletionRequest(
                    model=agent.model,
                    messages=build_title_messages(user_text, assistant_text),
                    system=TITLE_SYSTEM_PROMPT,
                    max_tokens=TITLE_MAX_TOKENS,
                )
            )
            title = clean_title(response.text)
        except (LLMError, AppError) as exc:
            # Deliberately not re-raised: a title is a nicety, and a job that
            # kept failing would re-enqueue and re-bill the same broken
            # provider. The fallback below is the answer instead.
            logger.warning(
                "conversation_title_generation_failed",
                conversation_id=conversation_id,
                error=type(exc).__name__,
            )

        # Always a write. A fallback that left `title` NULL would be
        # re-enqueued on the next turn, forever, for exactly the
        # conversations whose provider is broken.
        conversation.title = title or fallback_title(user_text)
        logger.info(
            "conversation_titled",
            conversation_id=conversation_id,
            generated=title is not None,
        )


async def import_products_task(
    ctx: dict[str, Any], *, organization_id: str, product_import_id: str
) -> None:
    """The arq entry point for Task 3's product import.

    Mirrors `ingest_document_task` above in every way that matters: it
    opens its own tenant-scoped session (there is no request here to have
    opened one), looks the import row up under `organization_id`'s RLS
    before reading anything off disk (so a job enqueued against the wrong
    organization fails here rather than reading another tenant's uploaded
    catalogue), and leaves the actual status-transition writes to
    `run_import` (`app/products/importer.py`) -- the same split as
    `ingest_document_task`/`ingest_document`, for the same reason: the
    session a failure happens on may itself be unusable afterward, so the
    function that knows the pipeline's own commit boundaries is the one
    that has to decide when to open a fresh one, not this wrapper.

    Deliberately absent from the log lines below: any row content. The
    whole point of `product_imports.errors` being a bounded, structured
    list (`RowError`) is undone if the same customer data goes to the
    operator's log instead.
    """
    tenant = TenantContext(
        organization_id=uuid.UUID(organization_id),
        user_id=None,
        role=None,
        request_id=f"product_import:{product_import_id}",
    )
    import_uuid = uuid.UUID(product_import_id)
    logger.info(
        "product_import_started",
        product_import_id=product_import_id,
        organization_id=organization_id,
    )
    started_at = time.monotonic()
    try:
        async with tenant_session(tenant) as session:
            record = await ProductImportService(session, tenant).get(import_uuid)
            mime_type = record.mime_type or ""
        data = load_import_bytes(tenant.organization_id, import_uuid)
        outcome = await run_import(tenant, import_uuid, data, mime_type)
    except Exception as exc:
        logger.error(
            "product_import_failed",
            product_import_id=product_import_id,
            organization_id=organization_id,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            error=bounded_error_message(exc),
        )
        raise
    logger.info(
        "product_import_completed",
        product_import_id=product_import_id,
        organization_id=organization_id,
        total_rows=outcome.total_rows,
        succeeded_count=outcome.succeeded_count,
        failed_count=outcome.failed_count,
        duration_ms=int((time.monotonic() - started_at) * 1000),
    )


async def run_evaluation_task(
    ctx: dict[str, Any], *, organization_id: str, evaluation_run_id: str
) -> None:
    """The arq entry point for Phase 6's evaluation runs (docs/PHASE-6.md
    §5) -- a thin wrapper, like `import_products_task` around `run_import`:
    `run_evaluation` (`app/evaluations/runner.py`) owns every session and
    status transition, because it alone knows which of its steps may have
    lost the session a failure happened on.

    The run row is looked up under `organization_id`'s RLS before anything
    else, so a job enqueued against the wrong organization finds nothing
    (`NotFoundError`) instead of running another tenant's dataset.

    Registered with its own 1-hour timeout (`WorkerSettings.functions`),
    not the worker-wide 10 minutes: cases run one after another, and 200 of
    them through a real provider can legitimately take that long -- and
    `run_evaluation` hands off to the next try (`arq.worker.Retry`) well
    before that timeout, so it needs arq's `job_try` from `ctx` and the
    `max_tries` it is registered with. The `evaluation_run_*` log lines come
    from `run_evaluation` itself and carry ids, counts and durations only --
    never a question or an answer.
    """
    tenant = TenantContext(
        organization_id=uuid.UUID(organization_id),
        user_id=None,
        role=None,
        request_id=f"evaluation_run:{evaluation_run_id}",
    )
    await run_evaluation(
        tenant,
        uuid.UUID(evaluation_run_id),
        job_try=int(ctx.get("job_try", 1)),
        max_tries=EVALUATION_MAX_TRIES,
    )

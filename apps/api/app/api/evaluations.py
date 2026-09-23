"""`POST /api/v1/evaluations/runs` -- starting an evaluation run
(docs/PHASE-6.md §5).

REST rather than a GraphQL mutation for exactly one reason (§7): a GraphQL
operation's transaction commits only after the resolver returns
(`app/graphql/context.py::build_context`), so a resolver that enqueued the
job would race its own commit -- the worker opens an independent connection
and can look for the run row before it exists. This handler commits first
and enqueues after, the same ordering `app/api/products.py` uses for an
import. Everything else about runs (listing, results, cancelling) is
GraphQL (Task 5).
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.auth.dependencies import get_current_tenant
from app.core.logging import get_logger
from app.core.rate_limit import enforce_rate_limit
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import EvalRun, EvalRunStatus
from app.evaluations.queue import enqueue_evaluation_run
from app.evaluations.schemas import StartRunInput
from app.evaluations.service import EvaluationService
from app.rag.ingest import bounded_error_message

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])

# Each run is up to 200 real, billed provider turns (plus a judge call per
# case with a reference answer), so starts are throttled per user on top of the one-active-run-per-
# dataset rule `create_run` enforces -- that rule alone still lets a caller
# start one run on every dataset they own at once.
START_RUN_RATE_LIMIT = 20
START_RUN_RATE_LIMIT_WINDOW_SECONDS = 3600


class EvalRunResponse(BaseModel):
    id: uuid.UUID
    dataset_id: uuid.UUID
    agent_id: uuid.UUID
    prompt_version_id: uuid.UUID | None
    provider: str
    model: str
    judge_provider: str | None
    judge_model: str | None
    status: EvalRunStatus
    case_count: int
    completed_count: int
    summary: dict[str, Any]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_model(cls, run: EvalRun) -> "EvalRunResponse":
        return cls(
            id=run.id,
            dataset_id=run.dataset_id,
            agent_id=run.agent_id,
            prompt_version_id=run.prompt_version_id,
            provider=run.provider,
            model=run.model,
            judge_provider=run.judge_provider,
            judge_model=run.judge_model,
            status=run.status,
            case_count=run.case_count,
            completed_count=run.completed_count,
            summary=run.summary,
            error=run.error,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    payload: StartRunInput,
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> EvalRunResponse:
    rate_limit_subject = tenant.user_id or tenant.organization_id
    async with tenant_session(tenant) as session:
        run = await EvaluationService(session, tenant).create_run(payload)
        # After validation, inside the transaction: only a start that would
        # actually create a run spends the limit (a 404/409/422 costs no
        # provider call), and a 429 here raises out of the block, so the
        # run is rolled back rather than committed. Unlike `chat_stream`,
        # which limits first because its work IS the provider call, the
        # expensive part here is the run itself -- validation is a few
        # indexed reads.
        await enforce_rate_limit(
            f"evaluation_run:{rate_limit_subject}",
            limit=START_RUN_RATE_LIMIT,
            window_seconds=START_RUN_RATE_LIMIT_WINDOW_SECONDS,
        )
        response = EvalRunResponse.from_model(run)

    # After the commit, not before -- `run_evaluation_task` looks this row up
    # through its own, independent session/connection, which cannot see a
    # row only flushed and not yet committed. See `import_products`'s
    # identical comment for the race this ordering avoids.
    try:
        await enqueue_evaluation_run(response.id, tenant.organization_id)
    except Exception as exc:
        # The row is already committed, so a failed enqueue (Redis down)
        # would otherwise leave a `pending` run no worker will ever pick up
        # -- and, since a pending run counts as active, the dataset locked
        # against every later start with a 409. Failing it frees the
        # dataset and says why.
        async with tenant_session(tenant) as session:
            await EvaluationService(session, tenant).mark_failed(
                response.id, f"could not queue the run: {bounded_error_message(exc)}"
            )
        logger.error(
            "evaluation_run_enqueue_failed",
            organization_id=str(tenant.organization_id),
            run_id=str(response.id),
            error=bounded_error_message(exc),
        )
        raise
    logger.info(
        "evaluation_run_accepted",
        organization_id=str(tenant.organization_id),
        run_id=str(response.id),
        dataset_id=str(response.dataset_id),
        case_count=response.case_count,
    )
    return response

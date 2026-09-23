"""Queueing an evaluation run -- `app/rag/queue.py::enqueue_ingest`'s
counterpart for Phase 6."""

import uuid

from app.workers.enqueue import enqueue
from app.workers.tasks import run_evaluation_task


async def enqueue_evaluation_run(run_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Queue `run_evaluation_task` for this run. Call only after the run's
    own transaction has committed -- see `app/api/evaluations.py`."""
    await enqueue(
        run_evaluation_task,
        organization_id=str(organization_id),
        evaluation_run_id=str(run_id),
    )

"""Dataset and case CRUD, plus read-only access to runs/results (Task 1),
and the run lifecycle (Task 4): `create_run`, `cancel_run`, and the
`claim_run`/`run_status`/`record_result`/`complete_run`/`mark_failed` steps
`app/evaluations/runner.py` drives -- see `docs/PHASE-6.md` §5.

The split with `runner.py` is business rules here, orchestration there: this
class decides what a status transition may do, what a result row and its
usage rows look like, and what the summary says; the runner decides which
session each step runs in, and in what order.

Every run-lifecycle write that can race another locks the run row first
(`_get_run_for_update`). `cancel_run` against `complete_run` is the race
that matters: without the lock, a cancel landing between the runner's
"still running?" read and its write would be silently overwritten by
`completed`.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.service import AgentService
from app.conversations.schemas import RecordUsageInput
from app.conversations.service import ConversationService
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import (
    Document,
    EvalCase,
    EvalDataset,
    EvalResult,
    EvalRun,
    EvalRunStatus,
    Product,
    PromptVersion,
)
from app.evaluations.schemas import (
    MAX_CASES_PER_DATASET,
    CaseInput,
    CreateDatasetInput,
    StartRunInput,
    UpdateDatasetInput,
)
from app.llm.registry import provider_is_configured, require_known_provider
from app.prompts.service import PromptService

_ACTIVE_STATUSES = (EvalRunStatus.PENDING, EvalRunStatus.RUNNING)

# `eval_runs.error` is rendered in the dashboard: a bounded message, never an
# unbounded exception dump.
_RUN_ERROR_MAX_CHARS = 1000

# Ceiling on `list_runs`'s `limit`, same reasoning as
# `ProductService._MAX_LIST_LIMIT`: an unbounded limit from an authenticated
# but otherwise untrusted caller is a full-table scan-and-serialize away.
_MAX_RUNS_LIMIT = 100


class EvaluationService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    async def list_datasets(self) -> list[EvalDataset]:
        result = await self.session.execute(
            select(EvalDataset)
            .where(EvalDataset.organization_id == self.tenant.organization_id)
            .order_by(EvalDataset.created_at.desc(), EvalDataset.id.desc())
        )
        return list(result.scalars().all())

    async def get_dataset(self, dataset_id: uuid.UUID) -> EvalDataset:
        result = await self.session.execute(
            select(EvalDataset).where(
                EvalDataset.id == dataset_id,
                EvalDataset.organization_id == self.tenant.organization_id,
            )
        )
        dataset = result.scalar_one_or_none()
        if dataset is None:
            # Cross-tenant lookups fail the same way a nonexistent id does --
            # see the identical note on DocumentService.get/ProductService.get.
            raise NotFoundError("dataset not found")
        return dataset

    async def create_dataset(self, data: CreateDatasetInput) -> EvalDataset:
        dataset = EvalDataset(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            name=data.name,
            description=data.description,
        )
        self.session.add(dataset)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # `uq_eval_dataset_org_name` is what actually enforces this --
            # relying on it (rather than a SELECT-then-INSERT pre-check) is
            # what keeps two concurrent creates of the same name from both
            # racing past a check and one of them surfacing a raw
            # IntegrityError as an unhandled 500. Same pattern as
            # AgentService.create_agent.
            raise ConflictError(f"a dataset named '{data.name}' already exists") from exc
        return dataset

    async def update_dataset(self, dataset_id: uuid.UUID, data: UpdateDatasetInput) -> EvalDataset:
        dataset = await self.get_dataset(dataset_id)
        if data.name is not None:
            dataset.name = data.name
        if data.description is not None:
            dataset.description = data.description
        try:
            await self.session.flush()
        except IntegrityError as exc:
            # Same race as `create_dataset`: two concurrent renames onto the
            # same name must not let the second one raise a raw
            # IntegrityError. See AgentService.update_agent.
            raise ConflictError(f"a dataset named '{dataset.name}' already exists") from exc
        # `updated_at`'s `onupdate=func.now()` is a server-side expression:
        # flush() does not fetch its new value back, so the attribute is left
        # expired. Refreshed now for the same reason `create_run` refreshes
        # below -- the GraphQL resolver serialises this row before the
        # request's session closes, and an expired attribute accessed off
        # the async event loop raises `MissingGreenlet` rather than lazily
        # reloading.
        await self.session.refresh(dataset)
        return dataset

    async def delete_dataset(self, dataset_id: uuid.UUID) -> None:
        # `eval_cases.dataset_id` is `ON DELETE CASCADE` (0014_evaluations.py),
        # so deleting the dataset row is enough to cascade its cases -- same
        # pattern as DocumentService.delete relying on document_chunks' FK.
        dataset = await self.get_dataset(dataset_id)
        await self.session.delete(dataset)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    async def list_cases(self, dataset_id: uuid.UUID) -> list[EvalCase]:
        await self.get_dataset(dataset_id)  # ownership check
        result = await self.session.execute(
            select(EvalCase)
            .where(
                EvalCase.dataset_id == dataset_id,
                EvalCase.organization_id == self.tenant.organization_id,
            )
            .order_by(EvalCase.created_at, EvalCase.id)
        )
        return list(result.scalars().all())

    async def count_cases(self, dataset_id: uuid.UUID) -> int:
        await self.get_dataset(dataset_id)  # ownership check
        result = await self.session.execute(
            select(func.count())
            .select_from(EvalCase)
            .where(
                EvalCase.dataset_id == dataset_id,
                EvalCase.organization_id == self.tenant.organization_id,
            )
        )
        return result.scalar_one()

    async def _validate_expected_ids(self, data: CaseInput) -> None:
        """Every `expected_document_ids`/`expected_product_ids` entry must
        belong to this organization. Neither array carries a foreign key
        (Postgres has none for "one of these ids per element"), so this is
        the FK-bypass ownership check applied to columns that have no FK to
        bypass in the first place -- see `EvalCase`'s docstring.

        The error names only the *count* of unknown ids, never which ones --
        echoing an id that exists in another organization would confirm its
        existence to a caller who should not be able to tell."""
        if data.expected_document_ids:
            result = await self.session.execute(
                select(Document.id).where(
                    Document.id.in_(data.expected_document_ids),
                    Document.organization_id == self.tenant.organization_id,
                )
            )
            found = {row for row in result.scalars().all()}
            unknown_count = len(set(data.expected_document_ids) - found)
            if unknown_count:
                raise ValidationError(
                    f"{unknown_count} expected_document_ids do not belong to this organization"
                )
        if data.expected_product_ids:
            result = await self.session.execute(
                select(Product.id).where(
                    Product.id.in_(data.expected_product_ids),
                    Product.organization_id == self.tenant.organization_id,
                )
            )
            found = {row for row in result.scalars().all()}
            unknown_count = len(set(data.expected_product_ids) - found)
            if unknown_count:
                raise ValidationError(
                    f"{unknown_count} expected_product_ids do not belong to this organization"
                )

    async def create_case(self, dataset_id: uuid.UUID, data: CaseInput) -> EvalCase:
        # Scoped ownership SELECT before the FK write -- Postgres FK checks
        # run with elevated privilege and bypass this session's RLS, so this
        # is what turns a cross-tenant dataset_id into NotFoundError instead
        # of a silently-written cross-tenant reference (see
        # DocumentService.replace_chunks for the identical pattern).
        await self.get_dataset(dataset_id)
        if await self.count_cases(dataset_id) >= MAX_CASES_PER_DATASET:
            raise ValidationError(f"a dataset may have at most {MAX_CASES_PER_DATASET} cases")
        await self._validate_expected_ids(data)
        case = EvalCase(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            dataset_id=dataset_id,
            question=data.question,
            reference_answer=data.reference_answer,
            required_phrases=data.required_phrases,
            expected_tool_names=data.expected_tool_names,
            expected_document_ids=data.expected_document_ids,
            expected_product_ids=data.expected_product_ids,
            tags=data.tags,
        )
        self.session.add(case)
        await self.session.flush()
        return case

    async def _get_case(self, case_id: uuid.UUID) -> EvalCase:
        result = await self.session.execute(
            select(EvalCase).where(
                EvalCase.id == case_id,
                EvalCase.organization_id == self.tenant.organization_id,
            )
        )
        case = result.scalar_one_or_none()
        if case is None:
            raise NotFoundError("case not found")
        return case

    async def update_case(self, case_id: uuid.UUID, data: CaseInput) -> EvalCase:
        """A full replace, not a patch -- every field in `data` overwrites
        the stored row, matching `CaseInput`'s own shape (there is no
        separate `UpdateCaseInput`)."""
        case = await self._get_case(case_id)
        await self._validate_expected_ids(data)
        case.question = data.question
        case.reference_answer = data.reference_answer
        case.required_phrases = data.required_phrases
        case.expected_tool_names = data.expected_tool_names
        case.expected_document_ids = data.expected_document_ids
        case.expected_product_ids = data.expected_product_ids
        case.tags = data.tags
        await self.session.flush()
        # Same reasoning as `update_dataset`'s refresh above.
        await self.session.refresh(case)
        return case

    async def delete_case(self, case_id: uuid.UUID) -> None:
        case = await self._get_case(case_id)
        await self.session.delete(case)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Runs / results -- reads
    # ------------------------------------------------------------------

    async def list_runs(
        self, dataset_id: uuid.UUID | None = None, limit: int = 50
    ) -> list[EvalRun]:
        limit = max(1, min(limit, _MAX_RUNS_LIMIT))
        query = select(EvalRun).where(EvalRun.organization_id == self.tenant.organization_id)
        if dataset_id is not None:
            query = query.where(EvalRun.dataset_id == dataset_id)
        query = query.order_by(EvalRun.created_at.desc(), EvalRun.id.desc()).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_run(self, run_id: uuid.UUID) -> EvalRun:
        result = await self.session.execute(
            select(EvalRun).where(
                EvalRun.id == run_id,
                EvalRun.organization_id == self.tenant.organization_id,
            )
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise NotFoundError("run not found")
        return run

    async def list_results(self, run_id: uuid.UUID) -> list[EvalResult]:
        await self.get_run(run_id)  # ownership check
        result = await self.session.execute(
            select(EvalResult)
            .where(
                EvalResult.run_id == run_id,
                EvalResult.organization_id == self.tenant.organization_id,
            )
            .order_by(EvalResult.created_at, EvalResult.id)
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Run lifecycle (Task 4) -- docs/PHASE-6.md §5
    # ------------------------------------------------------------------

    async def _get_run_for_update(self, run_id: uuid.UUID) -> EvalRun:
        result = await self.session.execute(
            select(EvalRun)
            .where(
                EvalRun.id == run_id,
                EvalRun.organization_id == self.tenant.organization_id,
            )
            .with_for_update()
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise NotFoundError("run not found")
        return run

    async def create_run(self, data: StartRunInput) -> EvalRun:
        """Validate and pin a new `pending` run (docs/PHASE-6.md §5).

        The dataset row is locked (`FOR UPDATE`) before the "no other active
        run" check, so two concurrent starts of the same dataset serialise on
        it: the second waits for the first to commit, then sees its run and
        gets `ConflictError`, instead of both passing the check. That lock is
        also this method's scoped ownership SELECT for the `dataset_id` FK;
        `get_agent` and `get_version` are the ones for `agent_id` and
        `prompt_version_id`.
        """
        dataset_result = await self.session.execute(
            select(EvalDataset)
            .where(
                EvalDataset.id == data.dataset_id,
                EvalDataset.organization_id == self.tenant.organization_id,
            )
            .with_for_update()
        )
        dataset = dataset_result.scalar_one_or_none()
        if dataset is None:
            raise NotFoundError("dataset not found")

        case_count = await self.count_cases(dataset.id)
        if case_count == 0:
            raise ValidationError("a dataset needs at least one case to run")
        if case_count > MAX_CASES_PER_DATASET:
            raise ValidationError(
                f"a run may cover at most {MAX_CASES_PER_DATASET} cases; "
                f"this dataset has {case_count}"
            )

        agent = await AgentService(self.session, self.tenant).get_agent(data.agent_id)

        # Pinned now, not resolved per case: a version activated halfway
        # through a run must not split it in two (§5).
        prompts = PromptService(self.session, self.tenant)
        prompt_version_id: uuid.UUID | None
        if data.prompt_version_id is not None:
            version = await prompts.get_version(data.prompt_version_id)
            if agent.prompt_id is None or version.prompt_id != agent.prompt_id:
                raise ValidationError("prompt version does not belong to this agent's prompt")
            prompt_version_id = version.id
        elif agent.prompt_id is not None:
            prompt_version_id = (await prompts.active_version(agent.prompt_id)).id
        else:
            # No prompt at all: the default system prompt answers, which has
            # no version to record -- the same `None` a live turn records.
            prompt_version_id = None

        provider = data.provider or agent.provider
        model = data.model or agent.model
        _require_usable_provider(provider)
        if data.judge_provider is not None:
            _require_usable_provider(data.judge_provider)

        active = await self.session.execute(
            select(EvalRun.id)
            .where(
                EvalRun.dataset_id == dataset.id,
                EvalRun.organization_id == self.tenant.organization_id,
                EvalRun.status.in_(_ACTIVE_STATUSES),
            )
            .limit(1)
        )
        if active.scalar_one_or_none() is not None:
            raise ConflictError("this dataset already has a run in progress")

        run = EvalRun(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            dataset_id=dataset.id,
            agent_id=agent.id,
            prompt_version_id=prompt_version_id,
            provider=provider,
            model=model,
            judge_provider=data.judge_provider,
            judge_model=data.judge_model,
            status=EvalRunStatus.PENDING,
            case_count=case_count,
            completed_count=0,
            summary={},
            triggered_by=self.tenant.user_id,
        )
        self.session.add(run)
        await self.session.flush()
        # `created_at`/`updated_at` are server defaults; loaded now so the
        # caller can serialise the row without a lazy load on a closed
        # session.
        await self.session.refresh(run)
        return run

    async def cancel_run(self, run_id: uuid.UUID) -> EvalRun:
        """`pending`/`running` -> `cancelled`; a terminal run is returned
        unchanged. The runner re-reads the status between cases, so a
        running run stops after the case in flight (§5)."""
        run = await self._get_run_for_update(run_id)
        if run.status in _ACTIVE_STATUSES:
            run.status = EvalRunStatus.CANCELLED
            run.finished_at = datetime.now(UTC)
            await self.session.flush()
        return run

    async def recorded_case_ids(self, run_id: uuid.UUID) -> set[uuid.UUID]:
        """The cases that already hold a result for this run -- what lets a
        retried run resume instead of re-running (and re-billing) them."""
        await self.get_run(run_id)  # ownership check
        result = await self.session.execute(
            select(EvalResult.case_id).where(
                EvalResult.run_id == run_id,
                EvalResult.organization_id == self.tenant.organization_id,
                EvalResult.case_id.is_not(None),
            )
        )
        return {case_id for case_id in result.scalars().all() if case_id is not None}

    async def claim_run(self, run_id: uuid.UUID) -> EvalRun | None:
        """Step 1 of a run: `pending`/`running` -> `running`, `started_at`
        set once. `running` is accepted too, since that is what a run whose
        worker died part-way looks like to arq's retry. Returns `None` for a
        terminal run -- nothing to do."""
        run = await self._get_run_for_update(run_id)
        if run.status not in _ACTIVE_STATUSES:
            return None
        run.status = EvalRunStatus.RUNNING
        if run.started_at is None:
            run.started_at = datetime.now(UTC)
        await self.session.flush()
        return run

    async def run_status(self, run_id: uuid.UUID) -> EvalRunStatus:
        return (await self.get_run(run_id)).status

    async def record_result(self, data: "RecordResultInput") -> bool:
        """Insert one case's result and, only if it was actually inserted,
        its `usage_events` rows and the run's `completed_count` increment.

        `ON CONFLICT (run_id, case_id) DO NOTHING`: a retry racing a result
        already written for this case is a no-op rather than an
        `IntegrityError`, and -- because the usage rows are skipped with it
        -- never a second bill for the same case.

        Returns `False` (and writes nothing) for a conflict, or for a case
        deleted while the run was in flight: a result with `case_id` NULL
        would escape the unique constraint, so a retry could duplicate it.
        """
        # Scoped ownership SELECTs before every FK this INSERT establishes
        # (run_id, case_id, prompt_version_id) -- FK checks bypass RLS.
        await self.get_run(data.run_id)
        case_exists = await self.session.execute(
            select(EvalCase.id).where(
                EvalCase.id == data.case_id,
                EvalCase.organization_id == self.tenant.organization_id,
            )
        )
        if case_exists.scalar_one_or_none() is None:
            return False
        prompt_version_id = data.prompt_version_id
        if prompt_version_id is not None:
            version_exists = await self.session.execute(
                select(PromptVersion.id).where(
                    PromptVersion.id == prompt_version_id,
                    PromptVersion.organization_id == self.tenant.organization_id,
                )
            )
            if version_exists.scalar_one_or_none() is None:
                # Deleted since the turn ran (`ON DELETE SET NULL` would
                # have nulled it anyway).
                prompt_version_id = None

        statement = (
            pg_insert(EvalResult)
            .values(
                id=uuid7(),
                organization_id=self.tenant.organization_id,
                run_id=data.run_id,
                case_id=data.case_id,
                question=data.question,
                answer=data.answer,
                error=data.error,
                scores=data.scores,
                passed=data.passed,
                tool_calls=data.tool_calls,
                cited_document_ids=data.cited_document_ids,
                cited_product_ids=data.cited_product_ids,
                prompt_version_id=prompt_version_id,
                latency_ms=data.latency_ms,
                input_tokens=data.input_tokens,
                output_tokens=data.output_tokens,
                cost_usd=data.cost_usd,
            )
            .on_conflict_do_nothing(constraint="uq_eval_result_run_case")
            .returning(EvalResult.id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none() is not None
        if not inserted:
            return False

        await self.session.execute(
            update(EvalRun)
            .where(
                EvalRun.id == data.run_id,
                EvalRun.organization_id == self.tenant.organization_id,
            )
            .values(completed_count=EvalRun.completed_count + 1)
        )
        conversations = ConversationService(self.session, self.tenant)
        for usage in data.usage:
            await conversations.record_usage(usage)
        return True

    async def complete_run(self, run_id: uuid.UUID) -> EvalRun | None:
        """The last step: if the run is still `running`, write its summary
        from ALL its results (a resumed run's earlier attempt included) and
        mark it `completed`. A run cancelled meanwhile is left exactly as
        the cancel left it -- the row lock is what makes that check-then-
        write safe against a concurrent `cancel_run`. Returns `None` when
        nothing was completed."""
        run = await self._get_run_for_update(run_id)
        if run.status is not EvalRunStatus.RUNNING:
            return None
        results = await self.list_results(run_id)
        run.summary = build_summary(results)
        # Re-derived rather than trusted: a result written by a crashed
        # attempt whose increment never committed still counts.
        run.completed_count = len(results)
        run.status = EvalRunStatus.COMPLETED
        run.finished_at = datetime.now(UTC)
        await self.session.flush()
        return run

    async def mark_failed(self, run_id: uuid.UUID, error: str) -> None:
        """`pending`/`running` -> `failed`. A single conditional UPDATE, so
        a run already cancelled or completed is never overwritten, and a run
        id this organization does not own matches nothing."""
        await self.session.execute(
            update(EvalRun)
            .where(
                EvalRun.id == run_id,
                EvalRun.organization_id == self.tenant.organization_id,
                EvalRun.status.in_(_ACTIVE_STATUSES),
            )
            .values(
                status=EvalRunStatus.FAILED,
                error=error[:_RUN_ERROR_MAX_CHARS],
                finished_at=datetime.now(UTC),
            )
        )


@dataclass(frozen=True, slots=True)
class RecordResultInput:
    """One case's outcome as `record_result` writes it. `tool_calls` is
    already the stored JSON shape (`[{name, arguments, is_error, excerpt}]`)
    and `scores` already `score_to_json`-ed, so this module needs no import
    of the scorers."""

    run_id: uuid.UUID
    case_id: uuid.UUID
    question: str
    answer: str | None
    error: str | None
    scores: dict[str, Any]
    passed: bool
    tool_calls: list[dict[str, Any]]
    cited_document_ids: list[uuid.UUID]
    cited_product_ids: list[uuid.UUID]
    prompt_version_id: uuid.UUID | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    usage: list[RecordUsageInput] = field(default_factory=list)


def _require_usable_provider(name: str) -> None:
    require_known_provider(name)  # ValidationError for an unknown name
    if not provider_is_configured(name):
        raise ValidationError(f"provider '{name}' has no API key configured")


def build_summary(results: list[EvalResult]) -> dict[str, Any]:
    """The run summary docs/PHASE-6.md §5 defines, from every result.

    `scorers[key].mean` averages the numeric scores only -- a judge that
    errored has `score` NULL, which counts as applicable (and not passed)
    but has no number to average.

    `cost_usd` is the exact `Decimal` sum as a string, or `None` as soon as
    any result that reported usage has no cost (an unpriced model). A result
    whose turn errored before reporting any usage (`input_tokens` NULL)
    contributes nothing: no usage figure was billed, so there is no unknown
    price to hide.
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    errored = sum(1 for r in results if r.error is not None)

    scorer_scores: dict[str, list[float]] = {}
    scorer_passed: dict[str, int] = {}
    scorer_applicable: dict[str, int] = {}
    for result in results:
        for key, score in result.scores.items():
            scorer_applicable[key] = scorer_applicable.get(key, 0) + 1
            scorer_passed[key] = scorer_passed.get(key, 0) + (1 if score.get("passed") else 0)
            numeric = score.get("score")
            scorer_scores.setdefault(key, [])
            if numeric is not None:
                scorer_scores[key].append(float(numeric))
    scorers = {
        key: {
            "mean": (sum(scorer_scores[key]) / len(scorer_scores[key]))
            if scorer_scores[key]
            else None,
            "passed": scorer_passed[key],
            "applicable": scorer_applicable[key],
        }
        for key in sorted(scorer_applicable)
    }

    cost: Decimal | None = Decimal(0)
    for result in results:
        if result.cost_usd is None:
            if result.input_tokens is None and result.error is not None:
                continue
            cost = None
            break
        assert cost is not None
        cost += result.cost_usd

    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return {
        "passed": passed,
        "failed": total - passed,
        "errored": errored,
        "pass_rate": (passed / total) if total else None,
        "scorers": scorers,
        "cost_usd": str(cost) if cost is not None else None,
        "mean_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
    }

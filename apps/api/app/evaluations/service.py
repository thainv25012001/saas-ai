"""Dataset and case CRUD, plus read-only access to runs/results (Task 1).

Task 4 adds this same class's run-execution methods (create/cancel/claim/
record) -- see `docs/PHASE-6.md` §5 and the plan's `task-4-brief.md`. Method
names here are final; Task 4 must not rename them.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Document, EvalCase, EvalDataset, EvalResult, EvalRun, Product
from app.evaluations.schemas import (
    MAX_CASES_PER_DATASET,
    CaseInput,
    CreateDatasetInput,
    UpdateDatasetInput,
)

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
        return case

    async def delete_case(self, case_id: uuid.UUID) -> None:
        case = await self._get_case(case_id)
        await self.session.delete(case)
        await self.session.flush()

    # ------------------------------------------------------------------
    # Runs / results -- read-only here. Task 4 adds the writers.
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

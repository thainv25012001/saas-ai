import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class EvalRunStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvalDataset(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A named collection of cases -- see `docs/PHASE-6.md` §3 for the
    schema. `UNIQUE (organization_id, name)` is what `EvaluationService.
    create_dataset` turns into a `ConflictError` rather than a duplicate row.
    """

    __tablename__ = "eval_datasets"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_eval_dataset_org_name"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class EvalCase(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One question plus the expectations a run scores it against.

    `expected_document_ids`/`expected_product_ids` are plain arrays, not
    foreign keys -- Postgres has no FK type for "one of these ids per
    element" -- so `EvaluationService` validates every entry against this
    organization's own `documents`/`products` on every write instead, the
    same FK-bypass reasoning `docs/ARCHITECTURE.md` §2.3 applies elsewhere.
    `expected_tool_names` (`app/tools/{retrieve,leads,products}.py` names)
    has no table of its own to check against, so it is validated at the
    schema layer instead (`app/evaluations/schemas.py::CaseInput`).
    """

    __tablename__ = "eval_cases"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    # Judged (docs/PHASE-6.md §4's `judge` scorer), never string-matched --
    # `required_phrases` below is the deterministic check.
    reference_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_phrases: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    expected_tool_names: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    expected_document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    expected_product_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)


class EvalRun(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One execution of a dataset against a pinned agent/prompt/model pair.

    Task 1 only reads this table (`list_runs`/`get_run`); Task 4 adds the
    methods that create and advance one. `prompt_version_id`/`provider`/
    `model`/`judge_provider`/`judge_model` are all pinned at start
    (docs/PHASE-6.md §5) so a later change elsewhere (activating a new
    prompt version, editing the agent) cannot retroactively change what an
    in-flight or already-finished run measured.
    """

    __tablename__ = "eval_runs"

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_datasets.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    judge_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    judge_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[EvalRunStatus] = mapped_column(
        SAEnum(
            EvalRunStatus,
            name="eval_run_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=EvalRunStatus.PENDING,
    )
    case_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvalResult(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One case's outcome within a run -- denormalised (`question` is
    copied, not joined) because a result outlives its case: `case_id` is
    `ON DELETE SET NULL`, and docs/PHASE-6.md §3 wants a result that still
    reads as a complete record after the case that produced it is deleted or
    edited.

    `UNIQUE (run_id, case_id)` is what makes an arq retry of a crashed run
    resumable (Task 4's `ON CONFLICT (run_id, case_id) DO NOTHING`): a case
    already holding a result for this run is never re-run or re-billed.

    `TimestampMixin` gives this append-only table `updated_at` too, even
    though docs/PHASE-6.md §3 lists only `created_at` -- same deviation, same
    reasoning, as `Message`/`UsageEvent` (see `Conversation`'s docstring):
    the blanket "created_at/updated_at on every table" rule wins over one
    table's specific field list, and nothing ever writes `updated_at` after
    insert.
    """

    __tablename__ = "eval_results"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="uq_eval_result_run_case"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("eval_cases.id", ondelete="SET NULL"),
        nullable=True,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scores: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    tool_calls: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    cited_document_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    cited_product_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    prompt_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("prompt_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Nullable, matching `Message`'s identical columns: a case can error
    # (a provider timeout, a step-limit hit) before ever producing usage to
    # report, and NULL says "no figure" rather than a misleading 0.
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

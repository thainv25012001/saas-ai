"""evaluations

Revision ID: 0014_evaluations
Revises: 0013_seed_product_tools

Task 1 (docs/PHASE-6.md §3): `eval_datasets`, `eval_cases`, `eval_runs` and
`eval_results` -- the schema Task 4's worker writes into and Task 5's
GraphQL surface reads from. This migration only creates the tables; nothing
here seeds data (unlike 0009/0013), because a dataset/case/run/result all
start out empty until an operator (or Task 4's runner) creates one.

Every array column (`required_phrases`, `expected_tool_names`,
`expected_document_ids`, `expected_product_ids`, `tags`,
`cited_document_ids`, `cited_product_ids`) defaults to `'{}'`, matching
`agent_configs.enabled_tool_names` (0003_agents.py). `expected_document_ids`/
`expected_product_ids` carry no foreign key at all -- Postgres has no FK
type for "one of these ids per array element" -- so `EvaluationService`
validates every entry against this organization's own `documents`/`products`
on every write instead (docs/PHASE-6.md §3, and the identical FK-bypass
reasoning behind the scoped ownership SELECTs elsewhere in this codebase).
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0014_evaluations"
down_revision = "0013_seed_product_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "eval_datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_eval_dataset_org_name"),
    )
    enable_rls(op, "eval_datasets")

    op.create_table(
        "eval_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        # Judged, not string-matched -- renamed from ARCHITECTURE.md §3.7's
        # `expected_answer` (docs/PHASE-6.md §3, marked ★).
        sa.Column("reference_answer", sa.Text(), nullable=True),
        sa.Column(
            "required_phrases", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "expected_tool_names",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "expected_document_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "expected_product_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Every case read goes through `EvaluationService.list_cases`/
    # `count_cases`, both filtered on exactly (organization_id, dataset_id) --
    # RLS's own predicate plus the explicit Layer 1 organization_id, per
    # docs/ARCHITECTURE.md §2.3.
    op.create_index(
        "ix_eval_cases_organization_id_dataset_id",
        "eval_cases",
        ["organization_id", "dataset_id"],
    )
    enable_rls(op, "eval_cases")

    op.create_table(
        "eval_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dataset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_datasets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Pinned at start (docs/PHASE-6.md §5): a version activated, or an
        # agent's provider/model changed, mid-run must not retroactively
        # change what an in-flight or already-finished run measured.
        sa.Column(
            "prompt_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prompt_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("judge_provider", sa.String(50), nullable=True),
        sa.Column("judge_model", sa.String(100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "running",
                "completed",
                "failed",
                "cancelled",
                name="eval_run_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "triggered_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # `list_runs`'s own access pattern: every organization's runs for a
    # dataset (or all of them), newest first. DESC on `created_at` so the
    # index itself already matches that order instead of Postgres scanning
    # ascending and reversing.
    op.execute(
        "CREATE INDEX ix_eval_runs_organization_id_dataset_id_created_at "
        "ON eval_runs (organization_id, dataset_id, created_at DESC)"
    )
    # Partial: only a `pending`/`running` row can ever block a new run of
    # the same dataset (docs/PHASE-6.md §5's "no other run of the same
    # dataset is pending or running"), so a completed/failed/cancelled run
    # -- the overwhelming majority, over time -- never needs an entry here.
    # Task 4's `create_run` is the reader; not used within Task 1.
    op.execute(
        "CREATE INDEX ix_eval_runs_active_by_dataset ON eval_runs (dataset_id) "
        "WHERE status IN ('pending', 'running')"
    )
    enable_rls(op, "eval_runs")

    op.create_table(
        "eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: a result outlives the case that produced it
        # (docs/PHASE-6.md §3) -- `question`/`answer`/`scores` below already
        # hold everything a transcript would be read for, denormalised.
        sa.Column(
            "case_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("eval_cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("scores", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tool_calls", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "cited_document_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "cited_product_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "prompt_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prompt_versions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # What makes a retried arq job resumable (docs/PHASE-6.md §3): Task
        # 4's writer uses `ON CONFLICT (run_id, case_id) DO NOTHING`, so a
        # case already holding a result for this run is never re-run or
        # re-billed.
        sa.UniqueConstraint("run_id", "case_id", name="uq_eval_result_run_case"),
    )
    enable_rls(op, "eval_results")


def downgrade() -> None:
    disable_rls(op, "eval_results")
    op.drop_table("eval_results")
    disable_rls(op, "eval_runs")
    op.drop_table("eval_runs")
    op.execute("DROP TYPE IF EXISTS eval_run_status")
    disable_rls(op, "eval_cases")
    op.drop_table("eval_cases")
    disable_rls(op, "eval_datasets")
    op.drop_table("eval_datasets")

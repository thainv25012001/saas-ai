"""product imports

Revision ID: 0012_product_imports
Revises: 0011_product_embedding_model

Task 3's import record (docs/PHASE-5.md §5): a persisted row per CSV/JSON
catalogue upload, not a response-only object. `POST /api/v1/products/import`
answers 202 before any row has actually landed, and the arq job that does
the real work runs long after that response is gone -- an in-memory or
response-only record would have nowhere to put "row 12 failed because its
price was not a number" by the time anyone could read it. See
`app/products/importer.py` for the write side and `app/db/models/product_import.py`
for the column-by-column rationale.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0012_product_imports"
down_revision = "0011_product_embedding_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "product_imports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The uploaded file's own name, for the dashboard to show next to
        # this row -- purely informational, never used to look anything up.
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "completed",
                "failed",
                name="product_import_status",
            ),
            nullable=False,
            server_default="pending",
        ),
        # NULL until parsing finishes -- the row count is not known before
        # that, and NULL (rather than 0) is what tells a client "still
        # counting" apart from "an empty file".
        sa.Column("total_rows", sa.Integer(), nullable=True),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        # list[{"row": int, "external_id": str | None, "message": str}] --
        # see app/products/importer.py::RowError. jsonb, not a child table:
        # nothing ever queries into this list, it is only ever read whole,
        # once, by the customer who ran this one import.
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default="[]"),
        # A whole-file failure (bad top-level JSON, a CSV missing a required
        # column) -- `documents.error`'s counterpart, populated instead of
        # `errors` when there was never a row to report on individually.
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_product_imports_organization_id_created_at",
        "product_imports",
        ["organization_id", "created_at"],
    )
    enable_rls(op, "product_imports")


def downgrade() -> None:
    disable_rls(op, "product_imports")
    op.drop_table("product_imports")
    op.execute("DROP TYPE IF EXISTS product_import_status")

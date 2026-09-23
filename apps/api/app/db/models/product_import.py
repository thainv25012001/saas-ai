import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ProductImportStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProductImport(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One CSV/JSON catalogue upload -- Task 3's import record
    (`docs/PHASE-5.md` §5).

    Persisted, not response-only: `POST /api/v1/products/import` answers 202
    before a single row has landed, and the arq job that does the real work
    (`app/products/importer.py::run_import`) finishes long after that
    response is gone. A customer coming back later to find out why row 12
    failed needs somewhere durable to read that from -- an in-memory or
    single-response record would already be gone by the time the errors
    exist to report.

    `status` follows the same shape as `Document.status`
    (pending -> processing -> completed/failed), but this row also carries
    counts and a per-row error list that a single document's status column
    has no equivalent of -- an import is a batch of up to thousands of
    independently-succeeding-or-failing rows, not one entity with one
    outcome, so reusing `Document`'s shape unchanged would have meant
    bolting a `errors` list onto a table that otherwise means one thing.
    """

    __tablename__ = "product_imports"

    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[ProductImportStatus] = mapped_column(
        SAEnum(
            ProductImportStatus,
            name="product_import_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ProductImportStatus.PENDING,
    )
    # NULL until parsing finishes (see the migration's comment) --
    # `run_import` sets this and the two counts below together, in the same
    # write, so a reader never observes one without the others.
    total_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # list[{"row": int, "external_id": str | None, "message": str}], 1-based
    # and header-aware for a CSV (row 1 is the header, so the first data row
    # is row 2 -- the number a human looking at their own spreadsheet would
    # point at) -- see app/products/importer.py::RowError.
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    # A whole-file failure -- bad top-level JSON, a CSV missing a required
    # column, or an unhandled exception -- populated instead of `errors`
    # when there was never a row to report on individually.
    # `Document.error`'s counterpart.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

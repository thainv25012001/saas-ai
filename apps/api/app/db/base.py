import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import uuid7


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid7)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Every tenant-owned table carries this. Pairing it with enable_rls() in
    the migration is what makes isolation structural rather than remembered.

    No `index=True` here: every migration in this plan creates its own
    explicit `ix_<table>_organization_id` index by hand, so that hand-written
    index is the single source of truth. A flag here would never take effect
    at runtime but would make a future `alembic revision --autogenerate`
    propose a phantom duplicate index.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )


def enable_rls(op: Any, table: str) -> None:
    """Turn on Row-Level Security for a tenant-owned table.

    The policy reads a per-transaction setting written by the tenant session
    dependency (see app/core/tenancy.py). The `true` second argument to
    current_setting makes a *virgin* backend -- one that has never run
    `SET LOCAL app.current_org_id` -- report it as NULL rather than raise.

    That is not the only case that matters. Once any transaction on a
    backend has set this custom GUC, the placeholder Postgres creates for
    it does not revert to NULL when that transaction ends: it reverts to
    the empty string ''. A pooled connection that has already served one
    tenant request is exactly this warmed state, and `''::uuid` raises
    `invalid input syntax for type uuid`, not "no rows". NULLIF(..., '')
    collapses that empty string back to NULL before the cast, so both a
    virgin backend and a warm, previously-tenanted one fail closed to zero
    rows instead of a 500.
    """
    guarded = "NULLIF(current_setting('app.current_org_id', true), '')::uuid"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (organization_id = {guarded}) "
        f"WITH CHECK (organization_id = {guarded})"
    )


def disable_rls(op: Any, table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

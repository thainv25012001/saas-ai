import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Supplier(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_suppliers_organization_name"),
        Index("ix_suppliers_organization_id", "organization_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sender_emails: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    sender_domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class SupplierMapping(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "supplier_mappings"
    __table_args__ = (
        UniqueConstraint("supplier_id", "version", name="uq_supplier_mappings_supplier_version"),
        Index("ix_supplier_mappings_organization_id", "organization_id"),
        Index("ix_supplier_mappings_supplier_id", "supplier_id"),
    )

    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    worksheet_name: Mapped[str] = mapped_column(String(128), nullable=False)
    header_row: Mapped[int] = mapped_column(Integer, nullable=False)
    column_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    default_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    default_purchase_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

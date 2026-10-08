import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
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
    #: Supplier SKU -> catalogue product id, remembered when an owner accepts
    #: a product match, so that SKU matches without review next time.
    sku_aliases: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict)


class SupplierMapping(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "supplier_mappings"
    __table_args__ = (
        UniqueConstraint("supplier_id", "version", name="uq_supplier_mappings_supplier_version"),
        Index("ix_supplier_mappings_organization_id", "organization_id"),
        Index("ix_supplier_mappings_supplier_id", "supplier_id"),
        Index("ix_supplier_mappings_source_message_id", "source_message_id"),
        CheckConstraint(
            "(source_message_id IS NULL) = (source_attachment_id IS NULL)",
            name="ck_supplier_mappings_source_identifiers_together",
        ),
    )

    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    worksheet_name: Mapped[str] = mapped_column(String(128), nullable=False)
    header_row: Mapped[int] = mapped_column(Integer, nullable=False)
    column_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    column_labels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: Every worksheet/header-row candidate in the source workbook as
    #: `{worksheet_name, header_row, labels}`: header labels only, never cell
    #: values, so an owner can move a proposal to the right sheet or row.
    sheet_options: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("supplier_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_attachment_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    default_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    default_purchase_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

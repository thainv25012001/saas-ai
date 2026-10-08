import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class SupplierMessage(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "supplier_messages"
    __table_args__ = (
        UniqueConstraint(
            "gmail_connection_id", "gmail_message_id", name="uq_supplier_messages_mailbox_message"
        ),
        Index("ix_supplier_messages_organization_id", "organization_id"),
        Index("ix_supplier_messages_supplier_status", "organization_id", "supplier_id", "status"),
    )

    gmail_connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("gmail_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True
    )
    gmail_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    history_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sender_email: Mapped[str] = mapped_column(String(320), nullable=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attachment_ids: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupplierQuote(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "supplier_quotes"
    __table_args__ = (
        UniqueConstraint(
            "supplier_message_id",
            "attachment_id",
            "worksheet_name",
            "source_row_number",
            name="uq_supplier_quotes_source_row",
        ),
        Index("ix_supplier_quotes_organization_id", "organization_id"),
        Index(
            "ix_supplier_quotes_supplier_product", "organization_id", "supplier_id", "product_id"
        ),
        Index("ix_supplier_quotes_source_message", "organization_id", "supplier_message_id"),
    )

    supplier_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    supplier_message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("supplier_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("supplier_mappings.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    attachment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    worksheet_name: Mapped[str] = mapped_column(String(128), nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    supplier_sku: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    specification: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    purchase_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    minimum_order_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    quantity_tiers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    lead_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    extraction_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unmatched")
    match_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    catalog_sale_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    catalog_sale_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    catalog_sale_unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    catalog_sale_price_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    profit_status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    unit_gross_profit: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    margin_percent: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    purchase_price_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ranking_basis: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    comparability_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class SupplierReport(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "supplier_reports"
    __table_args__ = (
        UniqueConstraint("supplier_message_id", name="uq_supplier_reports_message"),
        Index("ix_supplier_reports_organization_id", "organization_id"),
        Index("ix_supplier_reports_created_at", "organization_id", "created_at"),
    )

    supplier_message_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("supplier_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    profit_status: Mapped[str] = mapped_column(String(40), nullable=False)
    best_profit: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    best_margin_percent: Mapped[Decimal | None] = mapped_column(Numeric(7, 2), nullable=True)
    summary_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

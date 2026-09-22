import enum
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import CHAR, Boolean, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ProductAvailability(enum.StrEnum):
    IN_STOCK = "in_stock"
    OUT_OF_STOCK = "out_of_stock"
    PREORDER = "preorder"
    DISCONTINUED = "discontinued"


class Product(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """One item in an organization's catalogue -- see `docs/ARCHITECTURE.md`
    §3.4 for the schema and `docs/PHASE-5.md` §4 for why the embedding and
    `search_tsv` deliberately do not cover every column.

    `search_tsv` (generated, created only in the migration -- see
    0010_products.py -- so it has no ORM-mapped counterpart here, matching
    `DocumentChunk.content_tsv`) and `embedding` cover **name, description
    and category** (plus, for the embedding, stable attributes) only. They
    deliberately exclude `price`, `stock_quantity` and `availability`: those
    three change on their own schedule -- a nightly stock sync, a price
    update -- and including them would turn every one of those into a
    re-embed (an API call and a write) to keep the vector honest, for a
    semantic question ("something safe for my family") those fields never
    answered anyway. Excluding them makes that kind of change a plain
    `UPDATE`. See `ProductService.upsert_many` for the write-path
    consequence of this split.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_product_org_external_id"),
    )

    # The catalogue's own primary key for this item (SKU, product id, ...),
    # not this table's surrogate `id`. This is what `upsert_many` matches
    # on: a re-import of the same source system updates the matching row
    # instead of duplicating it -- see the UNIQUE constraint above.
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    slug: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # ISO 4217, e.g. "USD" -- fixed-width so a truncated or padded code
    # (a common CSV-export artifact) is a load error, not a silent 2- or
    # 4-character value sitting next to correct 3-character ones.
    currency: Mapped[str | None] = mapped_column(CHAR(3), nullable=True)
    # Heterogeneous, per-vertical facts a car dealership and an e-commerce
    # store have no shared schema for -- {"seats": 7, "fuel": "hybrid"} vs.
    # whatever a retailer's variant looks like. The GIN index in the
    # migration is what makes Task 4's jsonb filter arm a real index scan
    # rather than a sequential one.
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    availability: Mapped[ProductAvailability] = mapped_column(
        SAEnum(
            ProductAvailability,
            name="product_availability",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=ProductAvailability.IN_STOCK,
    )
    stock_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    # 1536 to match every other embedding column in this codebase (see
    # DocumentChunk.embedding) -- both embedders in app/embeddings/ produce
    # exactly this width, and pgvector fixes a column's dimension at
    # creation. Nullable: a product can exist (created by hand, or queued
    # for import) before anything has embedded it.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536), nullable=True)

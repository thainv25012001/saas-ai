"""products

Revision ID: 0010_products
Revises: 0009_seed_builtin_tools

See `docs/ARCHITECTURE.md` §3.4 for the schema and `docs/PHASE-5.md` §4 for
the rationale behind `search_tsv`'s column list.
"""

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import TSVECTOR

from alembic import op
from app.db.base import disable_rls, enable_rls

revision = "0010_products"
down_revision = "0009_seed_builtin_tools"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The catalogue's own key (SKU, product id, ...) -- not `id`. Paired
        # with the UNIQUE constraint below, this is what turns a re-import
        # of the same source system into an upsert instead of a duplicate
        # (Task 3).
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("slug", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(255), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "availability",
            sa.Enum(
                "in_stock",
                "out_of_stock",
                "preorder",
                "discontinued",
                name="product_availability",
            ),
            nullable=False,
            server_default="in_stock",
        ),
        sa.Column("stock_quantity", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("product_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        # 1536 to match every other embedding column (DocumentChunk.embedding
        # -- see 0006_documents.py). Nullable: a product can exist (created
        # by hand, or queued for import) before anything has embedded it.
        sa.Column("embedding", Vector(1536), nullable=True),
        # A SHA-256 of the exact text `embedding` was computed from (see
        # app/products/embedding_text.py::embeddable_text) as of whichever
        # write last set `embedding` -- NOT necessarily a hash of this row's
        # *current* name/description/attributes. `ProductService.upsert_many`
        # deliberately allows an embedding-less upsert (a price/stock sync,
        # docs/PHASE-5.md §4) to change those columns without re-embedding;
        # without this column, that leaves a stored vector silently
        # describing content that no longer exists -- undetectable by
        # anything, ever. `upsert_many` compares this against a freshly
        # computed hash on every embedding-less write and flips
        # `embedding_stale` below when they no longer match.
        sa.Column("embedding_source_hash", sa.String(64), nullable=True),
        sa.Column("embedding_stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Generated over name/description/category ONLY -- deliberately not
        # price, stock_quantity or availability. Those three change on their
        # own schedule (a nightly stock sync, a price update), and if they
        # fed this column every such change would force a rewrite of a
        # generated expression's inputs on every row touched; worse, the
        # embedding this column's search complements (ProductService /
        # Task 3's import) would need the same inputs, turning a plain price
        # UPDATE into a re-embed. Excluding them keeps that an ordinary
        # UPDATE -- see docs/PHASE-5.md §4. This will read as an omission to
        # the next person touching this column; it is not one.
        #
        # 'english', not 'simple' or the server default: Task 4's retrieval
        # queries this column with `websearch_to_tsquery('english', ...)`.
        # Any other configuration still populates this column and still
        # looks correct in a quick check -- it only fails silently at query
        # time, returning zero keyword hits while the vector arm keeps
        # answering (Phase 3 hit exactly this; see
        # tests/integration/test_product_service.py for the stemming test
        # that pins it).
        sa.Column(
            "search_tsv",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', "
                "coalesce(name, '') || ' ' || coalesce(description, '') || ' ' "
                "|| coalesce(category, ''))",
                persisted=True,
            ),
        ),
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
        sa.UniqueConstraint("organization_id", "external_id", name="uq_product_org_external_id"),
    )

    op.execute("CREATE INDEX ix_products_search_tsv ON products USING gin (search_tsv)")
    # GIN on the raw jsonb column (default operator class, not jsonb_path_ops):
    # Task 4's filter arm needs both containment (`@>`) and key-existence
    # (`?`) queries over heterogeneous per-vertical attributes, and
    # jsonb_path_ops only accelerates `@>`.
    op.execute("CREATE INDEX ix_products_attributes ON products USING gin (attributes)")
    op.execute(
        "CREATE INDEX ix_products_embedding ON products USING hnsw (embedding vector_cosine_ops)"
    )
    # One composite btree, not two: organization_id leads (every query is
    # tenant-scoped -- RLS plus the explicit predicate, per
    # docs/ARCHITECTURE.md §2.3), category is the common equality filter
    # after it, and price trails as the range predicate -- "SUV under
    # $30,000" is category-equality-then-price-range in one index scan. An
    # org-scoped price range with no category filter still gets
    # organization_id as a usable left prefix.
    op.create_index(
        "ix_products_organization_id_category_price",
        "products",
        ["organization_id", "category", "price"],
    )
    enable_rls(op, "products")


def downgrade() -> None:
    disable_rls(op, "products")
    op.drop_table("products")
    op.execute("DROP TYPE IF EXISTS product_availability")

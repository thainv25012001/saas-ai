"""product embedding_model

Revision ID: 0011_product_embedding_model
Revises: 0010_products

Adds `products.embedding_model`, `DocumentChunk.embedding_model`'s
counterpart (0006_documents.py) -- see the column comment on
`app/db/models/product.py::Product.embedding_model` for why a mixed-provider
catalogue needs this recorded per row rather than assumed from configuration.

Nullable, with no backfill: every existing row (there are none yet -- this
migration ships in the same phase that creates the table) has no embedding
either, so there is no model to name for it.
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_product_embedding_model"
down_revision = "0010_products"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("products", sa.Column("embedding_model", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("products", "embedding_model")

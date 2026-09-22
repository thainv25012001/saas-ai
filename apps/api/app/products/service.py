import uuid
from typing import cast

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models import Product
from app.products.schemas import ProductInput

# Ceiling on `list_products`'s `limit`, same reasoning as
# DocumentService._MAX_LIST_LIMIT: an unbounded limit from an authenticated
# but otherwise untrusted caller is a full-table scan-and-serialize away.
_MAX_LIST_LIMIT = 100

# Columns `upsert_many` overwrites unconditionally on conflict -- every
# column except `embedding`, which gets its own COALESCE (see
# `upsert_many`'s docstring), and `search_tsv`, which is a generated column
# Postgres recomputes itself and cannot appear in a SET list at all.
_UPSERT_COLUMNS = (
    "name",
    "slug",
    "description",
    "category",
    "price",
    "currency",
    "attributes",
    "availability",
    "stock_quantity",
    "image_url",
    "product_url",
    "is_active",
    "metadata",
)


class ProductService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create(self, data: ProductInput) -> Product:
        product = Product(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            external_id=data.external_id,
            name=data.name,
            slug=data.slug,
            description=data.description,
            category=data.category,
            price=data.price,
            currency=data.currency,
            attributes=data.attributes,
            availability=data.availability,
            stock_quantity=data.stock_quantity,
            image_url=data.image_url,
            product_url=data.product_url,
            is_active=data.is_active,
            metadata_=data.metadata,
            embedding=data.embedding,
        )
        self.session.add(product)
        await self.session.flush()
        return product

    async def get(self, product_id: uuid.UUID) -> Product:
        result = await self.session.execute(
            select(Product).where(
                Product.id == product_id,
                Product.organization_id == self.tenant.organization_id,
            )
        )
        product = result.scalar_one_or_none()
        if product is None:
            # Cross-tenant lookups fail the same way a nonexistent id does --
            # see the identical note on DocumentService.get.
            raise NotFoundError("product not found")
        return product

    async def list_products(
        self,
        *,
        category: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Product]:
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        offset = max(0, offset)
        query = select(Product).where(Product.organization_id == self.tenant.organization_id)
        if category is not None:
            query = query.where(Product.category == category)
        if is_active is not None:
            query = query.where(Product.is_active == is_active)
        query = query.order_by(Product.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def upsert_many(self, rows: list[ProductInput]) -> list[Product]:
        """Bulk `INSERT ... ON CONFLICT (organization_id, external_id) DO
        UPDATE`, the mechanism behind Task 3's "re-import is an upsert, not
        a duplicate" (docs/PHASE-5.md §5).

        Every column is unconditionally overwritten from the new row on
        conflict *except* `embedding`, which uses
        `COALESCE(excluded.embedding, products.embedding)`. That asymmetry
        is deliberate, not an oversight:

        - A row that changed semantically (name/description/category) has a
          freshly computed embedding to go with it, and `excluded.embedding`
          is that new vector -- it must replace the stale one, or search
          silently keeps ranking the row on text that no longer describes
          it.
        - A row that only changed price, stock or availability (this
          method's other, more common caller: a nightly price/stock sync)
          has no reason to have recomputed an embedding at all --
          docs/PHASE-5.md §4 is explicit that this must stay a plain
          `UPDATE`. Such a caller passes `embedding=None`, and
          `COALESCE` preserves whatever vector is already stored instead of
          nulling it out. Unconditionally doing `embedding =
          excluded.embedding` here would silently blank a good vector on
          every price-only sync; unconditionally excluding `embedding` from
          the SET list entirely would leave a stale vector after a real
          content edit. Neither plain option is safe -- COALESCE is what
          makes "no embedding supplied" mean "unchanged" rather than
          "cleared".

        `search_tsv` needs no entry here: it is a generated column and
        Postgres recomputes it from the row's own `name`/`description`/
        `category` on every INSERT and UPDATE, including this one.
        """
        if not rows:
            return []

        values = [
            {
                "id": uuid7(),
                "organization_id": self.tenant.organization_id,
                "external_id": row.external_id,
                "name": row.name,
                "slug": row.slug,
                "description": row.description,
                "category": row.category,
                "price": row.price,
                "currency": row.currency,
                "attributes": row.attributes,
                "availability": row.availability,
                "stock_quantity": row.stock_quantity,
                "image_url": row.image_url,
                "product_url": row.product_url,
                "is_active": row.is_active,
                "metadata": row.metadata,
                "embedding": row.embedding,
            }
            for row in rows
        ]

        # The raw Table, not the ORM class: passing the mapped class to
        # `pg_insert()` routes through SQLAlchemy's ORM-aware bulk-insert key
        # resolution, which resolves a `values()` dict key against the
        # class's *attribute* namespace -- and every declarative model
        # class already has a `metadata` attribute (its `Base.metadata`
        # registry), which collides with this table's `metadata` column and
        # fails with a confusing `AttributeError` deep in SQLAlchemy internals
        # rather than a clear error. Table-level Core keys sidestep that
        # namespace entirely: every key below is the real column name.
        table = cast(Table, Product.__table__)
        insert_stmt = pg_insert(table).values(values)
        set_ = {column: getattr(insert_stmt.excluded, column) for column in _UPSERT_COLUMNS}
        set_["embedding"] = func.coalesce(insert_stmt.excluded.embedding, table.c.embedding)
        set_["updated_at"] = func.now()
        upsert_stmt = insert_stmt.on_conflict_do_update(
            index_elements=[table.c.organization_id, table.c.external_id],
            set_=set_,
        ).returning(table.c.id)

        result = await self.session.execute(upsert_stmt)
        ids = [row[0] for row in result.all()]
        await self.session.flush()

        # Re-select through the ORM rather than trusting RETURNING's row
        # order (Postgres does not guarantee it matches the input list) --
        # the explicit organization_id predicate here is Layer 1 (§2.3),
        # same as every other read in this service, even though every id
        # just came from an insert this session made into this tenant.
        #
        # populate_existing=True: without it, a row already present in this
        # Session's identity map (an earlier upsert_many call in the same
        # transaction touched the same external_id) would be handed back
        # with its stale, already-loaded attribute values instead of the
        # ones this UPDATE just wrote -- the SQL would be correct and the
        # returned object would still lie.
        refreshed = await self.session.execute(
            select(Product)
            .where(
                Product.id.in_(ids),
                Product.organization_id == self.tenant.organization_id,
            )
            .execution_options(populate_existing=True)
        )
        return list(refreshed.scalars().all())

    async def delete(self, product_id: uuid.UUID) -> None:
        product = await self.get(product_id)
        await self.session.delete(product)
        await self.session.flush()

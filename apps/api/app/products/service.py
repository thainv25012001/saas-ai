import uuid
from typing import cast

from sqlalchemy import Table, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.tenancy import TenantContext
from app.db.models import Product, ProductAvailability
from app.products.embedding_text import hash_embeddable_text
from app.products.schemas import ProductInput

logger = get_logger(__name__)

# Ceiling on `list_products`'s `limit`, same reasoning as
# DocumentService._MAX_LIST_LIMIT: an unbounded limit from an authenticated
# but otherwise untrusted caller is a full-table scan-and-serialize away.
_MAX_LIST_LIMIT = 100

# Columns `upsert_many` overwrites unconditionally on conflict, taking
# whatever value it already computed in Python for that row -- everything
# except `embedding` itself, which gets its own SQL-level COALESCE (see
# `upsert_many`'s docstring), and `search_tsv`, a generated column Postgres
# recomputes itself and cannot appear in a SET list at all.
#
# `embedding_source_hash`/`embedding_stale` belong here, not with
# `embedding`: unlike the vector, their "preserve if omitted" case is
# already resolved to a concrete value before this statement is built (see
# the per-row loop in `upsert_many`), so they need no SQL-level COALESCE of
# their own. `embedding_model` is the third exception, alongside `embedding`
# itself (see the SQL-level COALESCE built below): it names the space
# `embedding` lives in, so it must be preserved or replaced in the same
# lockstep as the vector, not unconditionally overwritten from a row that
# may be an embedding-less price/stock sync with no model to report.
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
    "embedding_source_hash",
    "embedding_stale",
)


def _escape_like(value: str) -> str:
    """`value` as a literal inside an `ILIKE` pattern whose escape character
    is a backslash: the escape character itself first, then both
    wildcards."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ProductService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create(self, data: ProductInput) -> Product:
        # A hash iff there is an embedding to describe -- a brand-new row
        # with no embedding yet has nothing that can go stale. See
        # `upsert_many`'s docstring and `Product.embedding_source_hash`.
        source_hash = (
            hash_embeddable_text(data.name, data.description, data.attributes)
            if data.embedding is not None
            else None
        )
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
            embedding_source_hash=source_hash,
            embedding_model=data.embedding_model,
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
        search: str | None = None,
        category: str | None = None,
        availability: ProductAvailability | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Product]:
        """The dashboard's catalogue listing (Phase 5 Task 6).

        `search` is a plain case-insensitive substring match on `name` or
        `external_id` -- what someone scanning their own catalogue types
        ("camry", "CAM-0"), not the agent's three-way semantic search: a
        dashboard filter has no reason to spend an embedding call per
        keystroke, and a partial SKU is exactly what `search_tsv`'s
        whole-word stemming would miss. `%`, `_` and backslashes in it are
        escaped, so they are text to find rather than wildcards.

        `id` breaks ties in the ordering: an import chunk commits every row
        in one transaction, so they share a `created_at` to the microsecond,
        and without a total order `offset` paging could show a row twice
        and another never.
        """
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        offset = max(0, offset)
        query = select(Product).where(Product.organization_id == self.tenant.organization_id)
        if search is not None and search.strip():
            pattern = f"%{_escape_like(search.strip())}%"
            query = query.where(
                or_(
                    Product.name.ilike(pattern, escape="\\"),
                    Product.external_id.ilike(pattern, escape="\\"),
                )
            )
        if category is not None:
            query = query.where(Product.category == category)
        if availability is not None:
            query = query.where(Product.availability == availability)
        if is_active is not None:
            query = query.where(Product.is_active == is_active)
        query = (
            query.order_by(Product.created_at.desc(), Product.id.desc()).limit(limit).offset(offset)
        )
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def list_categories(
        self, *, active_only: bool = False, limit: int | None = None
    ) -> list[str]:
        """Every distinct, non-null category this organization's catalogue
        uses, alphabetically -- the options for the dashboard's category
        filter, so it offers only values that can match something.

        `active_only`/`limit` exist for `search_products`' no-results
        message (`app/tools/products.py`), which lists the categories a
        model can retry with: only categories an active product carries can
        ever match a search (`_Filters` pins `is_active = true`), and the
        list reaches a prompt, so it is bounded there rather than growing
        with the catalogue."""
        statement = select(Product.category).where(
            Product.organization_id == self.tenant.organization_id,
            Product.category.is_not(None),
        )
        if active_only:
            statement = statement.where(Product.is_active.is_(True))
        statement = statement.distinct().order_by(Product.category)
        if limit is not None:
            statement = statement.limit(max(1, limit))
        result = await self.session.execute(statement)
        return [category for category in result.scalars().all() if category is not None]

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

        `embedding_model` gets the identical `COALESCE`, for the identical
        reason: it names the space `embedding` lives in, so it has to move
        with the vector in lockstep, not be treated like the unconditionally
        overwritten columns above.

        `search_tsv` needs no entry here: it is a generated column and
        Postgres recomputes it from the row's own `name`/`description`/
        `category` on every INSERT and UPDATE, including this one.

        **The gap `COALESCE` opens, and how this closes it.** Preserving a
        stale `embedding` on purpose is only safe if staleness stays
        detectable. Without `embedding_source_hash`, an embedding-less
        upsert whose `name`/`description`/`attributes` actually changed
        (a caller that should have re-embedded but didn't, or a legitimate
        two-phase import mid-way between "metadata written" and "embedding
        job ran") leaves a vector describing content that no longer exists
        -- silently, with no exception, no log, and nothing to query for it.
        `docs/PHASE-5.md` §8 lists price/stock going stale; it does not
        cover this, because until this hash existed there was nothing to
        observe.

        So: every row's `embedding_source_hash`/`embedding_stale` are
        computed in Python before the statement is built (not left to
        `COALESCE`), using a pre-fetch of each `external_id`'s currently
        stored hash. `embedding_stale` is a live comparison recomputed on
        every write, not memoised state carried from the previous row: it
        is `stored_hash is not None and stored_hash != this_write's_content_hash`,
        nothing more. That matters for a row that drifted and was then
        edited back to matching content -- it must read fresh again on
        that same write, not stay flagged because it was flagged once
        before with nothing since to un-flag it.

        - `embedding` supplied: the new vector demonstrably matches the new
          content (the caller just computed one from it), so
          `embedding_source_hash` becomes that content's hash and
          `embedding_stale` clears.
        - `embedding` omitted and the new content's hash still matches what
          is stored: `embedding_stale` is false; nothing to report.
        - `embedding` omitted and the new content's hash does NOT match what
          is stored: the row is about to start (or continue) describing
          something its vector was never computed from.
          `embedding_stale` is set and a `product.embedding_stale` warning
          is logged with the organization and external id. This is a
          decision, not a default: **raising here was rejected** because
          Task 3's own architecture (`docs/PHASE-5.md` §5 -- upload returns
          immediately, an arq job embeds separately) makes "metadata
          updated, embedding not yet recomputed" a normal, expected
          transient state, not a caller error; raising would make that
          two-phase design impossible without every metadata-only caller
          first threading through an escape hatch. Recording keeps the
          write available and makes the state queryable
          (`WHERE embedding_stale`) for a reconciliation job instead of
          requiring one to recompute every row's hash from scratch.
        """
        if not rows:
            return []

        existing_rows = await self.session.execute(
            select(Product.external_id, Product.embedding_source_hash).where(
                Product.organization_id == self.tenant.organization_id,
                Product.external_id.in_([row.external_id for row in rows]),
            )
            # Two rows in this batch cannot both be "the" prior state for
            # one external_id, so this predicate is belt-and-braces, not
            # load-bearing -- kept for the same reason every other read in
            # this service states organization_id explicitly (§2.3, Layer 1).
        )
        prior_hash_by_external_id = {
            row.external_id: row.embedding_source_hash for row in existing_rows
        }

        values = []
        for row in rows:
            content_hash = hash_embeddable_text(row.name, row.description, row.attributes)
            if row.embedding is not None:
                # A fresh embedding demonstrably matches the content it was
                # just computed from.
                source_hash: str | None = content_hash
                stale = False
            else:
                # `embedding` (and therefore what it was computed from) is
                # unchanged by this write -- `source_hash` stays whatever it
                # already was (None for a brand-new row with no embedding
                # yet). `stale` is a live comparison, not remembered state:
                # it is recomputed from scratch on every write as "does the
                # content this row has right now match the text its stored
                # embedding was computed from", so a row that drifted and
                # was then edited BACK to matching content correctly clears
                # on this same write, with no separate "un-stale" path
                # needed.
                source_hash = prior_hash_by_external_id.get(row.external_id)
                stale = source_hash is not None and source_hash != content_hash
                if stale:
                    logger.warning(
                        "product.embedding_stale",
                        organization_id=str(self.tenant.organization_id),
                        external_id=row.external_id,
                        reason=(
                            "content changed on an embedding-less upsert; "
                            "stored embedding no longer matches"
                        ),
                    )

            values.append(
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
                    "embedding_model": row.embedding_model,
                    "embedding_source_hash": source_hash,
                    "embedding_stale": stale,
                }
            )

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
        # Same COALESCE, same reason: `embedding_model` names the space
        # `embedding` lives in, so an embedding-less write (`embedding=None`,
        # preserved above) must preserve the model that vector was actually
        # computed under too, rather than unconditionally overwriting it with
        # this row's `embedding_model` (`None` for that same embedding-less
        # write), which would leave a real, preserved vector with no
        # recorded provider at all.
        set_["embedding_model"] = func.coalesce(
            insert_stmt.excluded.embedding_model, table.c.embedding_model
        )
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

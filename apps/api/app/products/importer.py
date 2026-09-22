"""Task 3: turning an uploaded CSV or JSON catalogue into product rows and
vectors -- `docs/PHASE-5.md` §5.

Two things this module deliberately does NOT do, because Tasks 1 and 2
already built them:

- It never builds its own idempotence. `ProductService.upsert_many`'s
  `UNIQUE (organization_id, external_id)` / `ON CONFLICT` is what makes a
  re-import of the same file a no-op rather than a duplicate; this module
  only has to get the right `ProductInput` rows to it.
- It never embeds inline. `app/products/embedding.py::embed_and_store` is
  the second phase; every row this module upserts is handed to
  `needs_reembedding` afterward, and only the ones that actually need a
  vector (never embedded, or embedded from text that no longer matches --
  see that module's docstring for the three states) go to `embed_and_store`.
  A re-import whose content did not change re-embeds nothing.

**One arq job, not two.** The brief's own wording -- "the arq worker does
the parsing, validation, upsert and embedding" -- already names one job;
splitting parse+upsert and embed into two separately-queued jobs would add
an ordering dependency (the embed job must not start before the upsert job
commits) for no benefit, since `docs/PHASE-5.md` §5/§8 already treat
"metadata written, embedding not yet recomputed" as a normal, expected
transient state -- which is exactly what a crash between this job's own
upsert-commit and its own embed-commit produces, no second job required to
explain it. A future reconciliation job (`needs_reembedding_clause`) is
what would pick up rows left in that state, whether this job crashed
mid-way or was never run again for some other reason.

**Per-row validation, chunked writes, no giant single transaction and no
one-row-per-transaction either.** Every row is validated independently in
Python (`_build_product_input`, via `ProductInput`) before anything touches
the database, so one bad price never prevents the other 3,999 rows from
being considered at all -- this is what makes "batch-level success"
possible in the first place, not a database-level retry of anything.
Valid rows are then written in chunks of `_IMPORT_CHUNK_SIZE`, each chunk
its own `ProductService.upsert_many` call (one INSERT ... ON CONFLICT
statement) followed by embedding whatever in that chunk needs it, committed
together in one transaction per chunk. A transaction per row would turn a
4,000-row import into 4,000+ round trips for no isolation benefit --
`upsert_many` already validated every row in Python, so the only thing left
that can fail per-chunk is a genuine database error (a connection blip, a
constraint this module did not anticipate), and chunking bounds both how
much work such a failure discards (at most `_IMPORT_CHUNK_SIZE` rows, not
the whole file) and how large any single SQL statement gets (bounded
parameter count, bounded lock duration).
"""

import asyncio
import csv
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError, format_validation_errors
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.tenancy import TenantContext, tenant_session
from app.db.models import ProductImport, ProductImportStatus
from app.products.embedding import embed_and_store, needs_reembedding
from app.products.schemas import ProductInput
from app.products.service import ProductService
from app.rag.ingest import bounded_error_message

logger = get_logger(__name__)


class UnsupportedImportType(AppError):
    code = "unsupported_import_type"
    status_code = 422


class ImportParseError(AppError):
    """A whole-file structural problem, not a bad row -- a CSV missing a
    required column, or a JSON document whose top level is not an array.
    Raised before any per-row work starts, and deliberately not caught
    per-row: there is no row to attach the error to, and reporting the same
    "missing column" failure once per row would bury the one thing the
    customer actually needs to fix under thousands of copies of it."""

    code = "invalid_import_file"
    status_code = 422


# ---------------------------------------------------------------------------
# Mime type resolution -- the same "browser says nothing useful, fall back
# to the extension" shape as app/rag/extract.py::resolve_mime_type, kept as
# its own small copy rather than generalising that function: the two
# problems only look alike from a distance (a completely different
# EXTENSION_MIME_TYPES table, a completely different SUPPORTED set), and
# threading a table through a shared helper would couple an unrelated
# document-upload module to this one for a dozen lines saved.
# ---------------------------------------------------------------------------

SUPPORTED_IMPORT_MIME_TYPES: frozenset[str] = frozenset({"text/csv", "application/json"})

_GENERIC_MIME_TYPES: frozenset[str] = frozenset(
    {"", "application/octet-stream", "binary/octet-stream"}
)

_EXTENSION_MIME_TYPES: dict[str, str] = {
    ".csv": "text/csv",
    ".json": "application/json",
}


def resolve_import_mime_type(reported: str | None, filename: str | None) -> str:
    """What to treat this upload as -- see `resolve_mime_type`'s docstring
    in app/rag/extract.py for why the reported type wins whenever it is
    anything other than empty/octet-stream, and the extension is only ever
    a fallback for the browsers that report nothing useful."""
    reported = reported or ""
    if reported.lower() not in _GENERIC_MIME_TYPES:
        return reported
    if not filename:
        return reported
    _, _, suffix = filename.rpartition(".")
    return _EXTENSION_MIME_TYPES.get(f".{suffix.lower()}", reported)


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

# Every ProductInput field an import row may supply. Deliberately excludes
# `embedding`/`embedding_model`: an import's first phase always supplies
# neither (see the module docstring), so even a JSON source file that names
# them is not read -- `_build_product_input` below never looks at those two
# keys in `raw`, whatever it contains.
_KNOWN_FIELDS: tuple[str, ...] = (
    "external_id",
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

# `attributes`/`metadata` are jsonb: arbitrary shape, so a flat CSV cell
# carries them as a JSON-encoded string (e.g. `{"seats": 5, "fuel": "hybrid"}`)
# rather than this module inventing a column-splatting convention (a
# `attributes.seats` header naming scheme, say) that Task 1 never specified
# and nothing downstream expects. A JSON import supplies these as native
# objects already, so `_build_product_input` accepts either shape uniformly
# -- see its docstring.
_JSON_FIELDS: frozenset[str] = frozenset({"attributes", "metadata"})

# Optional scalar fields where an empty CSV cell means "not supplied" (None)
# rather than the literal empty string -- required for `price`/`stock_quantity`
# in particular, since pydantic would otherwise try (and fail) to parse ""
# as a Decimal/int. `external_id`/`name`/`slug` are deliberately NOT in this
# set: those are required, and an empty cell for one of them must surface as
# "external_id: at least 1 character", not silently become `None` and fail
# with a less legible "not a valid string" instead.
_BLANK_MEANS_NONE_FIELDS: frozenset[str] = frozenset(
    {"description", "category", "price", "currency", "stock_quantity", "image_url", "product_url"}
)

# `availability`/`is_active` both carry defaults on `ProductInput`
# (IN_STOCK / True); an empty cell should take that default rather than be
# forced through pydantic as an empty string or empty-string-turned-None,
# either of which would fail validation instead of falling back.
_DEFAULTED_FIELDS: frozenset[str] = frozenset({"availability", "is_active"})


@dataclass(frozen=True, slots=True)
class RowError:
    """One row's reason for not being imported. `row` is 1-based and, for a
    CSV, header-aware: row 1 is the header, so the first data row is row 2
    -- the number a human looking at their own spreadsheet would point at,
    not a 0-based data index that is off by one from what they see."""

    row: int
    external_id: str | None
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {"row": self.row, "external_id": self.external_id, "message": self.message}


@dataclass(frozen=True, slots=True)
class ParsedRow:
    row: int
    product: ProductInput


@dataclass(slots=True)
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    total_rows: int = 0


def _slugify(name: str) -> str:
    """A `slug` a customer's own catalogue almost never carries -- it is an
    internal concept, not a field a source ERP or spreadsheet has any
    reason to export. Auto-generated from `name` when the row omits it (or
    leaves it blank) so a slug-less import is not simply a 4,000-row hard
    error; a row that does supply one is trusted as-is (`ProductInput` puts
    no format constraint on it beyond length)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "product"


def _external_id_of(raw: dict[str, Any]) -> str | None:
    value = raw.get("external_id")
    if value is None or value == "":
        return None
    return str(value).strip()


def _describe_row_error(exc: Exception) -> str:
    if isinstance(exc, PydanticValidationError):
        return format_validation_errors(exc.errors())
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON: {exc}"
    return str(exc)


def _row_error(row: int, raw: dict[str, Any], exc: Exception) -> RowError:
    return RowError(row=row, external_id=_external_id_of(raw), message=_describe_row_error(exc))


def _build_product_input(raw: dict[str, Any]) -> ProductInput:
    """Build one row's `ProductInput` from its raw field values.

    `raw`'s values may be strings (every CSV cell) or already-native JSON
    types (a JSON import's own objects/numbers/booleans) -- both are
    accepted uniformly here rather than this module maintaining two
    separate builders, one per source format. Raises pydantic's
    `ValidationError` or `json.JSONDecodeError`; both are caught by this
    module's callers and turned into a `RowError`, never left to escape and
    abort the rest of the file.
    """
    fields: dict[str, Any] = {}
    for name in _KNOWN_FIELDS:
        if name not in raw:
            continue
        value = raw[name]
        if name in _JSON_FIELDS:
            if value in (None, ""):
                continue
            fields[name] = json.loads(value) if isinstance(value, str) else value
        elif name in _BLANK_MEANS_NONE_FIELDS:
            fields[name] = None if value in (None, "") else value
        elif name in _DEFAULTED_FIELDS:
            if value not in (None, ""):
                fields[name] = value
        else:
            # external_id/name/slug: pass through even when blank, so a
            # missing required field fails with pydantic's own "at least 1
            # character" message rather than being silently turned into
            # `None` first (see `_BLANK_MEANS_NONE_FIELDS`'s docstring).
            fields[name] = "" if value is None else value

    if not fields.get("slug"):
        fields["slug"] = _slugify(str(fields.get("name") or ""))

    return ProductInput(**fields)


def _parse_csv(data: bytes) -> ParseResult:
    # `utf-8-sig`: Excel's "CSV UTF-8" export prepends a BOM, which -- left
    # in place -- would land inside the *value* of the first header cell
    # ("﻿external_id"), silently making that column invisible to every
    # lookup below and turning "CSV is missing required column(s):
    # external_id" into a support ticket about a column that is right there
    # on screen.
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    header = set(reader.fieldnames or [])
    missing = {"external_id", "name"} - header
    if missing:
        raise ImportParseError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

    result = ParseResult()
    for row_number, raw_row in enumerate(reader, start=2):  # the header consumes row 1
        result.total_rows += 1
        # A short row (fewer cells than the header) fills the missing keys
        # with `None` (csv.DictReader's `restval`); a long one adds a
        # `None` key holding the overflow. Neither is a value any field
        # above expects, so both are normalised the same way every other
        # blank cell is.
        raw = {
            key: ("" if value is None else value)
            for key, value in raw_row.items()
            if key is not None
        }
        try:
            product = _build_product_input(raw)
        except (PydanticValidationError, json.JSONDecodeError) as exc:
            result.errors.append(_row_error(row_number, raw, exc))
            continue
        result.rows.append(ParsedRow(row=row_number, product=product))
    return result


def _parse_json(data: bytes) -> ParseResult:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ImportParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ImportParseError("JSON import must be an array of product objects")

    result = ParseResult()
    for index, item in enumerate(payload):
        row_number = index + 1  # no header row to offset past, unlike CSV
        result.total_rows += 1
        if not isinstance(item, dict):
            result.errors.append(
                RowError(
                    row=row_number, external_id=None, message="each item must be a JSON object"
                )
            )
            continue
        try:
            product = _build_product_input(item)
        except (PydanticValidationError, json.JSONDecodeError) as exc:
            result.errors.append(_row_error(row_number, item, exc))
            continue
        result.rows.append(ParsedRow(row=row_number, product=product))
    return result


def parse_import(data: bytes, mime_type: str) -> ParseResult:
    """Parse an uploaded catalogue into valid rows plus per-row errors.
    Never raises for a bad *row* -- only for a whole-file structural
    problem (`ImportParseError`) or a mime type this module was never
    taught (`UnsupportedImportType`, which the API layer should already
    have rejected via `SUPPORTED_IMPORT_MIME_TYPES` before this is ever
    called; kept here too so a caller that skips that check still fails
    loudly instead of silently returning zero rows)."""
    if mime_type == "text/csv":
        return _parse_csv(data)
    if mime_type == "application/json":
        return _parse_json(data)
    raise UnsupportedImportType(f"unsupported import type '{mime_type}'")


def _dedupe_by_external_id(rows: list[ParsedRow]) -> tuple[list[ParsedRow], list[RowError]]:
    """Keep the LAST occurrence of each `external_id` in file order --
    ordinary upsert semantics: a later row for the same SKU wins, the same
    way a second import of the same SKU would.

    This has to happen before chunking, not per chunk: `ProductService.
    upsert_many` issues one `INSERT ... ON CONFLICT` statement per call, and
    Postgres refuses to let a single statement affect the same conflict
    target twice ("ON CONFLICT DO UPDATE command cannot affect row a second
    time") -- two rows sharing an `external_id` inside the same chunk would
    abort that whole chunk, not just one of the two rows. Deduping globally,
    before the file is ever split into chunks, also means which rows get
    reported as a duplicate never depends on `_IMPORT_CHUNK_SIZE` -- an
    implementation detail the customer's file should not have to know
    about to predict its own import result.
    """
    by_external_id: dict[str, ParsedRow] = {}
    duplicate_errors: list[RowError] = []
    for parsed in rows:
        external_id = parsed.product.external_id
        prior = by_external_id.get(external_id)
        if prior is not None:
            duplicate_errors.append(
                RowError(
                    row=prior.row,
                    external_id=external_id,
                    message=(
                        f"external_id '{external_id}' also appears on row {parsed.row}; "
                        "only the later occurrence was imported"
                    ),
                )
            )
        by_external_id[external_id] = parsed
    return list(by_external_id.values()), duplicate_errors


# Rows per `upsert_many` call / transaction. Large enough that a multi
# thousand row catalogue lands in a handful of round trips, not one per
# row; small enough that one chunk's parameter count stays far under
# Postgres's 65535-per-statement limit (this table's ~15 upsert columns *
# 500 rows is ~7500) and that one failing chunk discards a bounded slice of
# the file rather than the whole thing.
_IMPORT_CHUNK_SIZE = 500


def _chunks(rows: list[ParsedRow], size: int) -> list[list[ParsedRow]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


async def _write_chunk(
    session: AsyncSession, tenant: TenantContext, chunk: list[ParsedRow]
) -> None:
    """Upsert one chunk and embed whatever in it actually needs a vector.

    `needs_reembedding` (app/products/embedding.py) is what keeps a
    re-import cheap: a row whose upsert changed nothing embeddable (a
    price-only sync riding along in the same file, or a genuinely identical
    re-import) comes back with its previous, still-current embedding and is
    filtered out here before `embed_and_store` ever sees it -- no wasted
    embedding call, no wasted write.
    """
    results = await ProductService(session, tenant).upsert_many(
        [parsed.product for parsed in chunk]
    )
    to_embed = [product for product in results if needs_reembedding(product)]
    await embed_and_store(session, tenant, to_embed)


async def _write_rows_individually(
    tenant: TenantContext, chunk: list[ParsedRow]
) -> tuple[int, list[RowError]]:
    """Fallback for a chunk whose batched `_write_chunk` call failed at the
    database for a reason `ProductInput` did not catch -- re-run one row at
    a time, each its own transaction, so the row(s) actually at fault are
    the only ones reported failed.

    This is what makes "one bad row does not abort the batch" hold for
    *any* database-level failure, not just the ones Python-level validation
    happens to anticipate today: convicting all 500 rows in a chunk for one
    row's fault (the original behaviour) would be exactly the failure mode
    the brief exists to prevent, just moved one layer down from where
    per-row validation closes it. Deliberately only reached from the
    chunk-level `except` in `run_import` -- a clean import never pays a
    per-row round trip, only a chunk that has already failed once does.

    If every row in the chunk fails identically (a connection outage, not
    one row's fault) this still produces the correct outcome: every row is
    reported failed, each with its own accurate message, rather than
    reported failed as a side effect of a chunk-mate's problem.
    """
    succeeded = 0
    errors: list[RowError] = []
    for parsed in chunk:
        try:
            async with tenant_session(tenant) as session:
                await _write_chunk(session, tenant, [parsed])
            succeeded += 1
        except Exception as exc:
            errors.append(
                RowError(
                    row=parsed.row,
                    external_id=parsed.product.external_id,
                    message=bounded_error_message(exc),
                )
            )
    return succeeded, errors


class ProductImportService:
    """CRUD for `product_imports` -- `DocumentService`'s status-transition
    idiom, applied to a row that also carries counts and a per-row error
    list `Document` has no equivalent of."""

    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create(self, *, filename: str | None, mime_type: str) -> ProductImport:
        record = ProductImport(
            id=uuid7(),
            organization_id=self.tenant.organization_id,
            filename=filename,
            mime_type=mime_type,
            status=ProductImportStatus.PENDING,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get(self, product_import_id: uuid.UUID) -> ProductImport:
        result = await self.session.execute(
            select(ProductImport).where(
                ProductImport.id == product_import_id,
                ProductImport.organization_id == self.tenant.organization_id,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            # Cross-tenant lookups fail the same way a nonexistent id does
            # -- see the identical note on DocumentService.get.
            raise NotFoundError("product import not found")
        return record

    async def mark_processing(self, product_import_id: uuid.UUID) -> ProductImport:
        record = await self.get(product_import_id)
        record.status = ProductImportStatus.PROCESSING
        await self.session.flush()
        return record

    async def mark_completed(
        self,
        product_import_id: uuid.UUID,
        *,
        total_rows: int,
        succeeded_count: int,
        failed_count: int,
        errors: list[dict[str, Any]],
    ) -> ProductImport:
        record = await self.get(product_import_id)
        record.status = ProductImportStatus.COMPLETED
        record.total_rows = total_rows
        record.succeeded_count = succeeded_count
        record.failed_count = failed_count
        record.errors = errors
        record.completed_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def mark_failed(self, product_import_id: uuid.UUID, message: str) -> ProductImport:
        record = await self.get(product_import_id)
        record.status = ProductImportStatus.FAILED
        record.error = message
        record.completed_at = datetime.now(UTC)
        await self.session.flush()
        return record


# ---------------------------------------------------------------------------
# Uploaded-file storage -- app/rag/storage.py's shape, under its own
# subdirectory rather than reusing that module's (document-keyed) functions
# directly: an import id and a document id are both plain uuids, so nothing
# would break by accident today, but nothing here should depend on the two
# id spaces never colliding either.
# ---------------------------------------------------------------------------


def _import_bytes_path(organization_id: uuid.UUID, product_import_id: uuid.UUID) -> Path:
    return (
        Path(get_settings().upload_dir)
        / "product_imports"
        / str(organization_id)
        / str(product_import_id)
    )


def store_import_bytes(
    organization_id: uuid.UUID, product_import_id: uuid.UUID, data: bytes
) -> Path:
    path = _import_bytes_path(organization_id, product_import_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def load_import_bytes(organization_id: uuid.UUID, product_import_id: uuid.UUID) -> bytes:
    return _import_bytes_path(organization_id, product_import_id).read_bytes()


async def enqueue_product_import(product_import_id: uuid.UUID, organization_id: uuid.UUID) -> None:
    """Queue `import_products_task` -- `app/rag/queue.py::enqueue_ingest`'s
    counterpart for Task 3, kept beside the rest of this module rather than
    a new `app/products/queue.py` for one two-line function.

    Imported lazily, not at module level: `app.workers.tasks` imports
    `ProductImportService`/`run_import`/`load_import_bytes` from *this*
    module to define `import_products_task` in the first place, so a
    top-level `from app.workers.tasks import import_products_task` here
    would be a real import cycle, not just an unused one.
    """
    from app.workers.enqueue import enqueue
    from app.workers.tasks import import_products_task

    await enqueue(
        import_products_task,
        organization_id=str(organization_id),
        product_import_id=str(product_import_id),
    )


# ---------------------------------------------------------------------------
# Orchestration -- the arq job body. `app/workers/tasks.py::import_products_task`
# is the thin arq entry point around this, exactly the way `ingest_document_task`
# wraps `app/rag/ingest.py::ingest_document`.
# ---------------------------------------------------------------------------


async def run_import(
    tenant: TenantContext, product_import_id: uuid.UUID, data: bytes, mime_type: str
) -> ProductImport:
    """Parse `data`, upsert every valid row in chunks, embed whatever needs
    it, and record the outcome on `product_imports`.

    Commit discipline mirrors `app/rag/ingest.py::ingest_document`, for the
    same reason: `status=processing` is committed through its own
    independent transaction up front, so it is an observable state rather
    than one hidden inside a transaction that might run for minutes; and on
    any failure -- a structural parse error, a cancellation, or anything
    else escaping the chunk loop below -- `status=failed` is committed
    through a second, independent transaction rather than reusing whatever
    session was open when the failure happened. That session may itself be
    the thing that is broken (a database error inside a chunk's own
    `tenant_session` has already closed *that* transaction by the time
    control reaches here), so nothing below ever tries to keep using it.

    Each chunk gets its own `tenant_session` and its own try/except. A
    chunk that fails is NOT simply reported as every one of its rows
    failing: Python-level validation (`ProductInput`, including its
    `Numeric(12, 2)`-mirroring `price` constraint) catches the overwhelming
    majority of bad input before a chunk is ever built, but it cannot catch
    every way a write can fail at the database -- a constraint this module
    never anticipated, a connection blip, a value that is valid `Decimal`
    but not valid for some column this module doesn't yet validate as
    tightly. Convicting all 500 rows in the chunk for one row's fault would
    be exactly the failure the brief exists to prevent, just moved one
    layer down from where the report first closed it. So a failing chunk is
    re-run through `_write_rows_individually`, one row per transaction,
    and only the row(s) that actually fail alone are reported failed --
    see that function's docstring. This fallback only runs on the error
    path: a clean import never pays a per-row round trip for it.
    """
    async with tenant_session(tenant) as session:
        await ProductImportService(session, tenant).mark_processing(product_import_id)

    try:
        parse_result = parse_import(data, mime_type)
        deduped_rows, duplicate_errors = _dedupe_by_external_id(parse_result.rows)
        errors: list[RowError] = [*parse_result.errors, *duplicate_errors]
        succeeded = 0
        failed = len(errors)

        for chunk in _chunks(deduped_rows, _IMPORT_CHUNK_SIZE):
            try:
                async with tenant_session(tenant) as session:
                    await _write_chunk(session, tenant, chunk)
                succeeded += len(chunk)
            except Exception as exc:
                logger.warning(
                    "product_import_chunk_failed_retrying_row_by_row",
                    product_import_id=str(product_import_id),
                    organization_id=str(tenant.organization_id),
                    rows=len(chunk),
                    error=bounded_error_message(exc),
                )
                row_succeeded, row_errors = await _write_rows_individually(tenant, chunk)
                succeeded += row_succeeded
                errors.extend(row_errors)
                failed += len(row_errors)
    except asyncio.CancelledError:
        async with tenant_session(tenant) as session:
            await ProductImportService(session, tenant).mark_failed(
                product_import_id,
                "import was cancelled before it finished -- most likely the worker's "
                "job timeout or a worker shutdown. Retrying is safe.",
            )
        raise
    except Exception as exc:
        async with tenant_session(tenant) as session:
            await ProductImportService(session, tenant).mark_failed(
                product_import_id, bounded_error_message(exc)
            )
        raise

    async with tenant_session(tenant) as session:
        return await ProductImportService(session, tenant).mark_completed(
            product_import_id,
            total_rows=parse_result.total_rows,
            succeeded_count=succeeded,
            failed_count=failed,
            errors=[error.to_dict() for error in errors],
        )

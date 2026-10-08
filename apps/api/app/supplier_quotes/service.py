from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Table, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.ids import uuid7
from app.core.tenancy import TenantContext
from app.db.models.supplier import Supplier, SupplierMapping
from app.db.models.supplier_quote import SupplierMessage, SupplierQuote, SupplierReport
from app.supplier_quotes.schemas import (
    CanonicalQuoteRow,
    SaveSupplierMappingInput,
    SupplierInput,
    WorkbookRowIssue,
)

_MAX_LIST_LIMIT = 100
_MAX_STORED_ROW_ISSUES = 100
_ALLOWED_MESSAGE_STATUSES = {
    "pending",
    "processing",
    "ignored",
    "review_required",
    "processed",
    "failed",
}
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(slots=True)
class QuoteUpsertResult:
    quotes: list[SupplierQuote]
    report: SupplierReport


class SupplierQuoteService:
    def __init__(self, session: AsyncSession, tenant: TenantContext) -> None:
        self.session = session
        self.tenant = tenant

    async def create_supplier(self, data: SupplierInput) -> Supplier:
        await self._assert_supplier_name_available(data.name)
        supplier = Supplier(
            organization_id=self.tenant.organization_id,
            name=data.name,
            sender_emails=data.sender_emails,
            sender_domains=data.sender_domains,
            is_active=data.is_active,
        )
        self.session.add(supplier)
        await self.session.flush()
        return supplier

    async def list_suppliers(self) -> list[Supplier]:
        result = await self.session.execute(
            select(Supplier)
            .where(Supplier.organization_id == self.tenant.organization_id)
            .order_by(Supplier.name, Supplier.id)
        )
        return list(result.scalars().all())

    async def update_supplier(self, supplier_id: Any, data: SupplierInput) -> Supplier:
        result = await self.session.execute(
            select(Supplier).where(
                Supplier.id == supplier_id,
                Supplier.organization_id == self.tenant.organization_id,
            )
        )
        supplier = result.scalar_one_or_none()
        if supplier is None:
            raise NotFoundError("supplier not found")
        await self._assert_supplier_name_available(data.name, exclude_id=supplier_id)
        supplier.name = data.name
        supplier.sender_emails = data.sender_emails
        supplier.sender_domains = data.sender_domains
        supplier.is_active = data.is_active
        await self.session.flush()
        return supplier

    async def save_mapping(
        self, supplier_id: Any, proposal: SaveSupplierMappingInput
    ) -> SupplierMapping:
        supplier = await self._get_supplier(supplier_id)
        if proposal.source_message_id is not None:
            message = await self._get_message(proposal.source_message_id)
            if message.supplier_id not in {None, supplier.id}:
                raise NotFoundError("supplier message not found")
            attachment_ids = {
                item.get("attachment_id")
                for item in message.attachment_ids
                if isinstance(item, dict)
            }
            if proposal.source_attachment_id not in attachment_ids:
                raise NotFoundError("supplier attachment not found")
            if message.supplier_id is None:
                message.supplier_id = supplier.id

        current_version = await self.session.scalar(
            select(func.max(SupplierMapping.version)).where(
                SupplierMapping.organization_id == self.tenant.organization_id,
                SupplierMapping.supplier_id == supplier.id,
            )
        )
        mapping = SupplierMapping(
            organization_id=self.tenant.organization_id,
            supplier_id=supplier.id,
            version=(current_version or 0) + 1,
            worksheet_name=proposal.mapping.worksheet_name,
            header_row=proposal.mapping.header_row,
            column_mapping=dict(proposal.mapping.columns),
            column_labels=proposal.column_labels,
            source_message_id=proposal.source_message_id,
            source_attachment_id=proposal.source_attachment_id,
            default_currency=proposal.mapping.default_currency,
            default_purchase_unit=proposal.mapping.default_purchase_unit,
            is_confirmed=proposal.is_confirmed,
        )
        self.session.add(mapping)
        await self.session.flush()
        return mapping

    async def save_mapping_proposal(
        self,
        supplier_id: Any,
        *,
        worksheet_name: str,
        header_row: int,
        column_mapping: dict[str, str],
        column_labels: list[str],
        source_message_id: Any,
        source_attachment_id: str,
    ) -> SupplierMapping:
        """Persist a validated, unconfirmed label-only proposal for review."""
        supplier = await self._get_supplier(supplier_id)
        message = await self._get_message(source_message_id)
        if message.supplier_id not in {None, supplier.id}:
            raise NotFoundError("supplier message not found")
        attachment_ids = {
            item.get("attachment_id") for item in message.attachment_ids if isinstance(item, dict)
        }
        if source_attachment_id not in attachment_ids:
            raise NotFoundError("supplier attachment not found")
        if not worksheet_name or len(worksheet_name) > 128 or not 1 <= header_row <= 25_000:
            raise ValidationError("invalid workbook mapping proposal")
        if len(column_labels) > 100 or any(not label.strip() for label in column_labels):
            raise ValidationError("invalid workbook column labels")
        allowed_fields = {
            "supplier_sku",
            "product_name",
            "specification",
            "unit_price",
            "currency",
            "unit",
            "minimum_order_quantity",
            "quantity_tiers",
            "lead_time",
            "valid_until",
        }
        if any(field not in allowed_fields for field in column_mapping):
            raise ValidationError("unsupported workbook mapping field")

        current_version = await self.session.scalar(
            select(func.max(SupplierMapping.version)).where(
                SupplierMapping.organization_id == self.tenant.organization_id,
                SupplierMapping.supplier_id == supplier.id,
            )
        )
        mapping = SupplierMapping(
            organization_id=self.tenant.organization_id,
            supplier_id=supplier.id,
            version=(current_version or 0) + 1,
            worksheet_name=worksheet_name,
            header_row=header_row,
            column_mapping=column_mapping,
            column_labels=[label.strip()[:128] for label in column_labels],
            source_message_id=message.id,
            source_attachment_id=source_attachment_id,
            default_currency=None,
            default_purchase_unit=None,
            is_confirmed=False,
        )
        self.session.add(mapping)
        message.supplier_id = supplier.id
        message.status = "review_required"
        await self.session.flush()
        return mapping

    async def latest_confirmed_mapping(self, supplier_id: Any) -> SupplierMapping | None:
        mappings = await self.list_confirmed_mappings(supplier_id)
        return mappings[0] if mappings else None

    async def list_confirmed_mappings(self, supplier_id: Any) -> list[SupplierMapping]:
        result = await self.session.execute(
            select(SupplierMapping)
            .where(
                SupplierMapping.organization_id == self.tenant.organization_id,
                SupplierMapping.supplier_id == supplier_id,
                SupplierMapping.is_confirmed.is_(True),
            )
            .order_by(SupplierMapping.version.desc(), SupplierMapping.id.desc())
        )
        return list(result.scalars().all())

    async def get_or_create_message(
        self,
        *,
        connection_id: Any,
        gmail_message_id: str,
        history_id: str,
        sender_email: str,
        received_at: datetime | None,
        attachment_ids: list[dict[str, Any]],
    ) -> tuple[SupplierMessage, bool]:
        result = await self.session.execute(
            select(SupplierMessage).where(
                SupplierMessage.organization_id == self.tenant.organization_id,
                SupplierMessage.gmail_connection_id == connection_id,
                SupplierMessage.gmail_message_id == gmail_message_id,
            )
        )
        message = result.scalar_one_or_none()
        if message is not None:
            should_process = message.status not in {
                "ignored",
                "review_required",
                "processed",
            }
            return message, should_process
        message = SupplierMessage(
            organization_id=self.tenant.organization_id,
            gmail_connection_id=connection_id,
            gmail_message_id=gmail_message_id,
            history_id=history_id,
            sender_email=sender_email[:320],
            received_at=received_at,
            attachment_ids=attachment_ids,
            status="processing",
        )
        self.session.add(message)
        await self.session.flush()
        return message, True

    async def record_processing_status(
        self,
        message_id: Any,
        *,
        status: str,
        error_code: str | None = None,
    ) -> SupplierMessage:
        if status not in _ALLOWED_MESSAGE_STATUSES:
            raise ValidationError("unsupported supplier message status")
        if error_code is not None and not _ERROR_CODE_RE.fullmatch(error_code):
            error_code = "processing_error"
        message = await self._get_message(message_id)
        message.status = status
        message.error_code = error_code
        if status in {"processed", "ignored", "review_required", "failed"}:
            message.processed_at = datetime.now(UTC)
        await self.session.flush()
        return message

    async def upsert_quote_rows(
        self,
        supplier_id: Any,
        message_id: Any,
        attachment_id: str,
        rows: list[CanonicalQuoteRow],
        *,
        mapping_id: Any | None = None,
        row_issues: list[WorkbookRowIssue] | None = None,
    ) -> QuoteUpsertResult:
        supplier = await self._get_supplier(supplier_id)
        message = await self._get_message(message_id)
        if message.supplier_id not in {None, supplier.id}:
            raise NotFoundError("supplier message not found")
        attachment_ids = {
            item.get("attachment_id") for item in message.attachment_ids if isinstance(item, dict)
        }
        if attachment_id not in attachment_ids:
            raise NotFoundError("supplier attachment not found")
        if mapping_id is not None:
            mapping = await self._get_mapping(mapping_id)
            if mapping.supplier_id != supplier.id:
                raise NotFoundError("supplier mapping not found")
        message.supplier_id = supplier.id

        values = [
            self._quote_values(
                supplier_id=supplier.id,
                message_id=message.id,
                attachment_id=attachment_id,
                mapping_id=mapping_id,
                quote=quote,
            )
            for quote in rows
        ]
        if values:
            table = cast(Table, SupplierQuote.__table__)
            statement = pg_insert(table).values(values)
            mutable_columns = {
                column.name: getattr(statement.excluded, column.name)
                for column in table.columns
                if column.name
                not in {
                    "id",
                    "organization_id",
                    "supplier_message_id",
                    "attachment_id",
                    "worksheet_name",
                    "source_row_number",
                    "created_at",
                }
            }
            await self.session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        table.c.supplier_message_id,
                        table.c.attachment_id,
                        table.c.worksheet_name,
                        table.c.source_row_number,
                    ],
                    set_=mutable_columns,
                )
            )
        await self.session.flush()

        quote_result = await self.session.execute(
            select(SupplierQuote)
            .where(
                SupplierQuote.organization_id == self.tenant.organization_id,
                SupplierQuote.supplier_id == supplier.id,
                SupplierQuote.supplier_message_id == message.id,
                SupplierQuote.attachment_id == attachment_id,
            )
            .order_by(SupplierQuote.worksheet_name, SupplierQuote.source_row_number)
        )
        quote_rows = list(quote_result.scalars().all())

        issues = row_issues or []
        summary = {
            "quote_count": len(quote_rows),
            "issue_count": len(issues),
            "row_issues": [
                {
                    "worksheet_name": issue.worksheet_name,
                    "source_row_number": issue.source_row_number,
                    "code": issue.code,
                    "message": issue.message,
                }
                for issue in issues[:_MAX_STORED_ROW_ISSUES]
            ],
        }
        report_result = await self.session.execute(
            select(SupplierReport).where(
                SupplierReport.organization_id == self.tenant.organization_id,
                SupplierReport.supplier_message_id == message.id,
            )
        )
        report = report_result.scalar_one_or_none()
        previous_attachments = (
            dict(report.summary_json.get("attachments", {})) if report is not None else {}
        )
        previous_attachments[attachment_id] = {
            "quote_count": len(quote_rows),
            "issue_count": len(issues),
            "row_issues": summary["row_issues"],
        }
        total_quote_count = await self.session.scalar(
            select(func.count(SupplierQuote.id)).where(
                SupplierQuote.organization_id == self.tenant.organization_id,
                SupplierQuote.supplier_message_id == message.id,
            )
        )
        total_issue_count = sum(
            int(attachment_summary.get("issue_count", 0))
            for attachment_summary in previous_attachments.values()
            if isinstance(attachment_summary, dict)
        )
        combined_summary = {
            "quote_count": int(total_quote_count or 0),
            "issue_count": total_issue_count,
            "row_issues": [
                issue
                for attachment_summary in previous_attachments.values()
                if isinstance(attachment_summary, dict)
                for issue in attachment_summary.get("row_issues", [])
            ][:_MAX_STORED_ROW_ISSUES],
            "attachments": previous_attachments,
        }
        has_issues = total_issue_count > 0
        if report is None:
            report = SupplierReport(
                organization_id=self.tenant.organization_id,
                supplier_message_id=message.id,
                profit_status="review_required" if has_issues else "pending",
                best_profit=None,
                best_margin_percent=None,
                summary_json=combined_summary,
            )
            self.session.add(report)
        else:
            report.profit_status = "review_required" if has_issues else "pending"
            report.best_profit = None
            report.best_margin_percent = None
            report.summary_json = combined_summary
        message.status = "review_required" if has_issues else "processed"
        message.error_code = "row_issues" if has_issues else None
        message.processed_at = datetime.now(UTC)
        await self.session.flush()
        return QuoteUpsertResult(quotes=quote_rows, report=report)

    async def list_reports(self, *, limit: int = 50, offset: int = 0) -> list[SupplierReport]:
        bounded_limit = min(max(limit, 1), _MAX_LIST_LIMIT)
        result = await self.session.execute(
            select(SupplierReport)
            .where(SupplierReport.organization_id == self.tenant.organization_id)
            .order_by(SupplierReport.created_at.desc(), SupplierReport.id.desc())
            .limit(bounded_limit)
            .offset(max(offset, 0))
        )
        return list(result.scalars().all())

    async def get_report(self, report_id: Any) -> tuple[SupplierReport, list[SupplierQuote]]:
        report_result = await self.session.execute(
            select(SupplierReport).where(
                SupplierReport.id == report_id,
                SupplierReport.organization_id == self.tenant.organization_id,
            )
        )
        report = report_result.scalar_one_or_none()
        if report is None:
            raise NotFoundError("supplier report not found")
        quote_result = await self.session.execute(
            select(SupplierQuote)
            .where(
                SupplierQuote.organization_id == self.tenant.organization_id,
                SupplierQuote.supplier_message_id == report.supplier_message_id,
            )
            .order_by(
                SupplierQuote.product_id,
                SupplierQuote.purchase_price_rank,
                SupplierQuote.supplier_id,
            )
        )
        return report, list(quote_result.scalars().all())

    async def _get_supplier(self, supplier_id: Any) -> Supplier:
        result = await self.session.execute(
            select(Supplier).where(
                Supplier.id == supplier_id,
                Supplier.organization_id == self.tenant.organization_id,
            )
        )
        supplier = result.scalar_one_or_none()
        if supplier is None:
            raise NotFoundError("supplier not found")
        return supplier

    async def _get_message(self, message_id: Any) -> SupplierMessage:
        result = await self.session.execute(
            select(SupplierMessage).where(
                SupplierMessage.id == message_id,
                SupplierMessage.organization_id == self.tenant.organization_id,
            )
        )
        message = result.scalar_one_or_none()
        if message is None:
            raise NotFoundError("supplier message not found")
        return message

    async def _get_mapping(self, mapping_id: Any) -> SupplierMapping:
        result = await self.session.execute(
            select(SupplierMapping).where(
                SupplierMapping.id == mapping_id,
                SupplierMapping.organization_id == self.tenant.organization_id,
            )
        )
        mapping = result.scalar_one_or_none()
        if mapping is None:
            raise NotFoundError("supplier mapping not found")
        return mapping

    async def _assert_supplier_name_available(
        self, name: str, *, exclude_id: Any | None = None
    ) -> None:
        statement = select(Supplier.id).where(
            Supplier.organization_id == self.tenant.organization_id,
            func.lower(Supplier.name) == name.casefold(),
        )
        if exclude_id is not None:
            statement = statement.where(Supplier.id != exclude_id)
        existing_id = await self.session.scalar(statement.limit(1))
        if existing_id is not None:
            raise ConflictError("a supplier with this name already exists")

    def _quote_values(
        self,
        *,
        supplier_id: Any,
        message_id: Any,
        attachment_id: str,
        mapping_id: Any | None,
        quote: CanonicalQuoteRow,
    ) -> dict[str, Any]:
        return {
            "id": uuid7(),
            "organization_id": self.tenant.organization_id,
            "supplier_id": supplier_id,
            "supplier_message_id": message_id,
            "mapping_id": mapping_id,
            "product_id": None,
            "attachment_id": attachment_id,
            "worksheet_name": quote.worksheet_name,
            "source_row_number": quote.source_row_number,
            "supplier_sku": quote.supplier_sku,
            "product_name": quote.product_name,
            "specification": quote.specification,
            "unit_price": quote.unit_price,
            "currency": quote.currency,
            "purchase_unit": quote.purchase_unit,
            "minimum_order_quantity": quote.minimum_order_quantity,
            "quantity_tiers": [tier.model_dump(mode="json") for tier in quote.quantity_tiers],
            "lead_time": quote.lead_time,
            "valid_until": quote.valid_until,
            "extraction_confidence": quote.extraction_confidence,
            "match_status": "unmatched",
            "profit_status": "pending",
            "ranking_basis": "none",
            "comparability_reasons": [],
        }

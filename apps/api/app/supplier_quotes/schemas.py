from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

WorkbookField = Literal[
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
]


class WorkbookMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worksheet_name: str = Field(min_length=1, max_length=128)
    header_row: int = Field(ge=1, le=25_000)
    columns: dict[WorkbookField, str]
    default_currency: str | None = Field(default=None, max_length=16)
    default_purchase_unit: str | None = Field(default=None, max_length=64)

    @field_validator("worksheet_name", "default_purchase_unit", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("default_currency", mode="before")
    @classmethod
    def normalize_default_currency(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("columns")
    @classmethod
    def strip_column_labels(cls, value: dict[WorkbookField, str]) -> dict[WorkbookField, str]:
        cleaned = {field: label.strip() for field, label in value.items()}
        if any(not label for label in cleaned.values()):
            raise ValueError("column labels must not be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_required_mapping_fields(self) -> WorkbookMapping:
        if "unit_price" not in self.columns:
            raise ValueError("a unit_price column is required")
        if not ({"product_name", "supplier_sku"} & self.columns.keys()):
            raise ValueError("map product_name or supplier_sku to identify a product")
        if "currency" not in self.columns and not self.default_currency:
            raise ValueError("map currency or configure an explicit default_currency")
        if "unit" not in self.columns and not self.default_purchase_unit:
            raise ValueError("map unit or configure an explicit default_purchase_unit")
        return self


class WorkbookSampleRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_row_number: int = Field(ge=1)
    values: list[str | int | float | bool | None]


class WorkbookSheetInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    max_row: int = Field(ge=0)
    candidate_header_rows: list[int]
    header_labels: dict[int, list[str | None]]
    sample_rows: list[WorkbookSampleRow]


class WorkbookRowIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worksheet_name: str | None = None
    source_row_number: int | None = Field(default=None, ge=1)
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=160)


class WorkbookInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheets: list[WorkbookSheetInspection]
    issues: list[WorkbookRowIssue] = Field(default_factory=list)


class QuantityTier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_quantity: Decimal = Field(gt=0)
    unit_price: Decimal = Field(gt=0)


class CanonicalQuoteRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worksheet_name: str
    source_row_number: int = Field(ge=1)
    supplier_sku: str | None = None
    product_name: str | None = None
    specification: str | None = None
    unit_price: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    purchase_unit: str = Field(min_length=1, max_length=64)
    minimum_order_quantity: Decimal | None = Field(default=None, ge=0)
    quantity_tiers: list[QuantityTier] = Field(default_factory=list)
    lead_time: str | None = None
    valid_until: date | None = None
    extraction_confidence: float = Field(default=1.0, ge=0, le=1)


class WorkbookParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[CanonicalQuoteRow]
    issues: list[WorkbookRowIssue]

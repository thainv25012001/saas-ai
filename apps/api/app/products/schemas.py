from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.db.models import ProductAvailability


class ProductInput(BaseModel):
    """The write shape for both `ProductService.create` and `.upsert_many`.

    `embedding` is optional in both, but means something different in each:
    on a fresh insert `None` simply means "not embedded yet" (a hand-created
    product, or one queued for an import job); on an upsert it means
    "unchanged, leave the stored vector alone" -- see the comment on
    `ProductService.upsert_many` for why that is the safe reading rather
    than "clear it".
    """

    external_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=500)
    slug: str = Field(min_length=1, max_length=500)
    description: str | None = None
    category: str | None = None
    price: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    attributes: dict[str, Any] = Field(default_factory=dict)
    availability: ProductAvailability = ProductAvailability.IN_STOCK
    stock_quantity: int | None = None
    image_url: str | None = None
    product_url: str | None = None
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None

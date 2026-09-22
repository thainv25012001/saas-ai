from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

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
    # `max_digits`/`decimal_places` mirror `products.price`'s actual column
    # type, `Numeric(12, 2)` (0010_products.py) -- 10 integer digits, 2
    # decimal. Without this, a value that fits `Decimal` but not the column
    # (e.g. an import row with a stray extra digit) passes validation here
    # and fails at the database instead, where Task 3's import can no
    # longer tell "this specific row's price is bad" apart from "this
    # chunk's write failed for some reason" -- see `app/products/importer.py`'s
    # module docstring on why a database-level failure is handled with a
    # fallback rather than relied on not to happen.
    price: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    attributes: dict[str, Any] = Field(default_factory=dict)
    availability: ProductAvailability = ProductAvailability.IN_STOCK
    stock_quantity: int | None = None
    image_url: str | None = None
    product_url: str | None = None
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None
    # `DocumentChunk.embedding_model`'s counterpart -- see
    # `Product.embedding_model`'s comment for why a mixed-provider catalogue
    # needs this recorded rather than assumed. Required to travel with
    # `embedding` (the validator below), never independently: a vector with
    # no recorded provider is exactly the "which embedding space is this"
    # ambiguity this column exists to remove, and a model name with no
    # vector describes nothing.
    embedding_model: str | None = None

    @model_validator(mode="after")
    def _embedding_and_model_travel_together(self) -> Self:
        if (self.embedding is None) != (self.embedding_model is None):
            raise ValueError("embedding and embedding_model must be set together, or not at all")
        return self

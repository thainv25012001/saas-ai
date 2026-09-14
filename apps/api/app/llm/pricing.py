from dataclasses import dataclass
from decimal import Decimal

from app.core.logging import get_logger
from app.llm.types import Usage

logger = get_logger(__name__)

# Prices are USD per MILLION tokens. Verified 2026-09-14 against the providers'
# public pricing pages. An entry going stale is a billing error, so the date above
# is part of the data, not decoration.
_MTOK = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal


MODEL_PRICING: dict[str, ModelPrice] = {
    # Anthropic
    "claude-opus-5": ModelPrice(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPrice(Decimal("2.00"), Decimal("10.00")),
    "claude-haiku-4-5": ModelPrice(Decimal("1.00"), Decimal("5.00")),
    # OpenAI
    "gpt-4o": ModelPrice(Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": ModelPrice(Decimal("0.15"), Decimal("0.60")),
    # Local/testing
    "fake-1": ModelPrice(Decimal("0"), Decimal("0")),
}


def estimate_cost(model: str, usage: Usage) -> Decimal | None:
    """Cost in USD, or None when the model is not priced.

    Returning None rather than 0 is deliberate: an unpriced model must show up as a
    NULL in the database, where it is visible, instead of as a free request, where it
    silently understates the bill.
    """
    price = MODEL_PRICING.get(model)
    if price is None:
        logger.warning("model_not_priced", model=model)
        return None

    return (
        Decimal(usage.input_tokens) * price.input_per_mtok
        + Decimal(usage.output_tokens) * price.output_per_mtok
    ) / _MTOK

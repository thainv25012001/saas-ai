from decimal import Decimal

from app.core.logging import get_logger

logger = get_logger(__name__)

# Prices are USD per MILLION tokens, mirroring `app/llm/pricing.py`. Verified
# 2026-09-17 against OpenAI's public pricing page. An entry going stale is a
# billing error, so the date above is part of the data, not decoration.
_MTOK = Decimal(1_000_000)

EMBEDDING_PRICING: dict[str, Decimal] = {
    "text-embedding-3-small": Decimal("0.02"),
    # The hashing embedder runs no request against any provider, so its
    # price is exactly zero rather than "unpriced" -- an ingest that never
    # leaves the process must not surface as a NULL cost to explain away.
    "hashing": Decimal("0"),
}


def estimate_embedding_cost(model: str, tokens: int) -> Decimal | None:
    """Cost in USD, or None when the model is not priced.

    Same contract as `app/llm/pricing.estimate_cost`: returning None rather
    than 0 is deliberate, so an unpriced model shows up as a NULL in the
    database -- visible -- instead of as a free request, which would
    silently understate the bill.
    """
    price = EMBEDDING_PRICING.get(model)
    if price is None:
        logger.warning("embedding_model_not_priced", model=model)
        return None

    return Decimal(tokens) * price / _MTOK

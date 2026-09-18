from decimal import Decimal

from app.embeddings.pricing import EMBEDDING_PRICING, estimate_embedding_cost


def test_cost_is_decimal_not_float():
    """Money in floats is how a billing column ends up with 0.30000000000000004."""
    cost = estimate_embedding_cost("text-embedding-3-small", 1_000_000)
    assert isinstance(cost, Decimal)


def test_cost_for_exactly_one_million_tokens_is_the_table_rate():
    price = EMBEDDING_PRICING["text-embedding-3-small"]
    assert estimate_embedding_cost("text-embedding-3-small", 1_000_000) == price


def test_hashing_costs_nothing():
    """The default embedder never leaves the process; a real Decimal(0)
    keeps it out of the "unpriced" NULL bucket, which would read as a
    billing gap rather than the free local computation it is."""
    assert estimate_embedding_cost("hashing", 1_000_000) == Decimal("0")


def test_unknown_model_returns_none_rather_than_guessing():
    """A missing price must be visible as NULL, not silently wrong."""
    assert estimate_embedding_cost("some-model-we-have-never-priced", 1_000) is None


def test_zero_tokens_costs_nothing():
    assert estimate_embedding_cost("text-embedding-3-small", 0) == Decimal("0")

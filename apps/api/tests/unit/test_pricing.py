from decimal import Decimal

from app.llm.pricing import MODEL_PRICING, estimate_cost
from app.llm.types import Usage


def test_cost_is_decimal_not_float():
    """Money in floats is how a billing column ends up with 0.30000000000000004."""
    cost = estimate_cost("gpt-4o-mini", Usage(input_tokens=1_000_000, output_tokens=0))
    assert isinstance(cost, Decimal)


def test_cost_for_exactly_one_million_input_tokens_is_the_table_rate():
    price = MODEL_PRICING["gpt-4o-mini"]
    cost = estimate_cost("gpt-4o-mini", Usage(input_tokens=1_000_000, output_tokens=0))
    assert cost == price.input_per_mtok


def test_cost_sums_input_and_output():
    price = MODEL_PRICING["gpt-4o-mini"]
    cost = estimate_cost("gpt-4o-mini", Usage(input_tokens=1_000_000, output_tokens=1_000_000))
    assert cost == price.input_per_mtok + price.output_per_mtok


def test_unknown_model_returns_none_rather_than_guessing():
    """A missing price must be visible as NULL, not silently wrong."""
    assert estimate_cost("some-model-we-have-never-priced", Usage()) is None


def test_zero_usage_costs_nothing():
    assert estimate_cost("gpt-4o-mini", Usage()) == Decimal("0")


def test_claude_opus_5_is_priced():
    """The Anthropic default model must be in the table or every Anthropic
    conversation records a NULL cost."""
    assert "claude-opus-5" in MODEL_PRICING

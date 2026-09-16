from decimal import Decimal

from app.llm.pricing import MODEL_PRICING, estimate_cost
from app.llm.registry import DEFAULT_MODELS
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


def test_every_default_model_is_priced():
    """Nothing enforces that DEFAULT_MODELS and the pricing rules agree. Adding
    a provider (or changing its default model) without a price would silently
    produce agents whose cost is permanently NULL. Asserted through
    `estimate_cost` rather than against `MODEL_PRICING` directly, because a
    model can now be priced by rule (`:free`) as well as by table entry."""
    unpriced = [model for model in DEFAULT_MODELS.values() if estimate_cost(model, Usage()) is None]
    assert unpriced == []


def test_every_offered_model_is_priced():
    """`app/llm/catalog.py` hand-maintains the models the dashboard offers, and
    its own comment says it mirrors this table -- but the model picker is a
    `<select>`, so anything listed there is a model a user can actually save.
    Without this, dropping a price leaves the dropdown offering a model whose
    `cost_usd` lands as NULL on every `usage_events` row."""
    from app.llm.catalog import _STATIC_MODELS

    offered = [option.id for options in _STATIC_MODELS.values() for option in options]
    assert [model for model in offered if estimate_cost(model, Usage()) is None] == []


def test_every_known_provider_has_a_catalog():
    """A provider in `KNOWN_PROVIDERS` that no catalog answers for renders an
    empty dropdown -- a form that cannot be saved, with nothing in the logs to
    say why. Cheaper to catch here than to explain there."""
    from app.llm.catalog import _LIVE_CATALOGS, _STATIC_MODELS
    from app.llm.registry import KNOWN_PROVIDERS

    assert set(KNOWN_PROVIDERS) == set(_STATIC_MODELS) | set(_LIVE_CATALOGS)


def test_openrouter_free_models_cost_nothing():
    """OpenRouter's free tier charges nothing, and its roster churns often
    enough that a hardcoded table entry per model would go stale. The `:free`
    suffix is the vendor's own marker, so it is the rule."""
    cost = estimate_cost(
        "z-ai/glm-5.2:free", Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    assert cost == Decimal("0")


def test_a_paid_openrouter_model_is_still_unpriced():
    """The counterpart to the rule above: only `:free` is free. A paid
    OpenRouter model we have no price for must stay NULL, not fall through
    the same branch and report a free conversation."""
    assert estimate_cost("z-ai/glm-5.2", Usage(input_tokens=1_000, output_tokens=1_000)) is None

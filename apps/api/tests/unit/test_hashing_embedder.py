import math

import pytest

from app.embeddings.hashing import HashingEmbedder

pytestmark = pytest.mark.anyio


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_embedding_has_the_declared_dimensions():
    embedder = HashingEmbedder()
    [vector] = await embedder.embed(["hello world"])
    assert len(vector) == embedder.dimensions == 1536


async def test_embedding_is_l2_normalized():
    """Normalized vectors make cosine similarity a plain dot product, which is
    what the retrieval SQL assumes."""
    embedder = HashingEmbedder()
    [vector] = await embedder.embed(["the quick brown fox jumps"])
    assert math.isclose(math.sqrt(sum(x * x for x in vector)), 1.0, rel_tol=1e-6)


async def test_the_same_text_embeds_identically():
    embedder = HashingEmbedder()
    first, second = await embedder.embed(["warranty coverage", "warranty coverage"])
    assert first == second


async def test_texts_sharing_vocabulary_score_higher_than_unrelated_ones():
    """THE test for this task. If this fails, every retrieval test downstream is
    measuring noise."""
    embedder = HashingEmbedder()
    related_a, related_b, unrelated = await embedder.embed(
        [
            "the hybrid engine improves fuel economy",
            "fuel economy is better with the hybrid engine",
            "warranty claims must be filed within thirty days",
        ]
    )
    assert _cosine(related_a, related_b) > _cosine(related_a, unrelated)


async def test_unrelated_texts_score_near_zero():
    embedder = HashingEmbedder()
    a, b = await embedder.embed(["hybrid engine fuel economy", "warranty claim filing"])
    assert _cosine(a, b) < 0.2


async def test_batch_order_is_preserved():
    embedder = HashingEmbedder()
    vectors = await embedder.embed(["alpha", "beta", "gamma"])
    [alpha_alone] = await embedder.embed(["alpha"])
    assert vectors[0] == alpha_alone
    assert len(vectors) == 3


async def test_empty_text_yields_a_zero_vector_rather_than_raising():
    """A chunk can be whitespace after normalization. It must not blow up the
    ingest of a 200-page document."""
    embedder = HashingEmbedder()
    [vector] = await embedder.embed(["   "])
    assert all(x == 0.0 for x in vector)


async def test_an_empty_batch_returns_an_empty_list():
    assert await HashingEmbedder().embed([]) == []


async def test_case_and_punctuation_do_not_change_the_embedding():
    embedder = HashingEmbedder()
    a, b = await embedder.embed(["Hybrid Engine!", "hybrid engine"])
    assert _cosine(a, b) > 0.99

import hashlib
import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    """A hashed bag-of-words embedder. No network, no model, no dependency.

    This is NOT a fake in the sense `FakeProvider` is. A chat fake only has to
    produce some text; an embedding fake has to produce vectors whose cosine
    similarity *means* something, because every retrieval test asks "did the
    right chunk come back?". Hashing each input to a random vector would answer
    that with noise, and every downstream retrieval test would pass without
    testing anything.

    So this computes a real, if modest, embedding: tokens are hashed into
    buckets, weighted by frequency, and the vector is L2-normalized. Two
    passages sharing vocabulary score high; unrelated ones score near zero.

    Its limit is worth stating: it is lexical, not semantic. "car" and
    "automobile" are unrelated to it. It sets a floor for retrieval quality and
    makes the pipeline demoable with no API key — it is not a demonstration of
    what real RAG does. `OpenAIEmbeddingProvider` is the swap, and because both
    emit 1536 dimensions, swapping is a re-embed rather than a migration.
    """

    name = "hashing"

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def _bucket(self, token: str) -> int:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        return int.from_bytes(digest, "big") % self.dimensions

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN.findall(text.lower()):
            vector[self._bucket(token)] += 1.0

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # A chunk can be empty after normalization. Returning zeros keeps a
            # 200-page ingest alive; the chunk simply never matches anything.
            return vector
        return [value / norm for value in vector]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

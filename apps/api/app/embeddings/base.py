from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Separate from `LLMProvider` on purpose.

    Anthropic offers no embeddings API, so an organization can reasonably run
    Claude for chat and something else for embeddings. Folding this into the
    chat protocol would have forced a rewrite the moment Anthropic was added.
    """

    name: str
    dimensions: int

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch, returning one vector per input in the same order.

        Batch-shaped because ingesting a document embeds hundreds of chunks,
        and one request per chunk is the difference between a fast ingest and
        a rate-limited one.
        """
        ...

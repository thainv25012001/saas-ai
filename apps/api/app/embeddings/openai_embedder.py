import openai

from app.llm.errors import (
    LLMConfigurationError,
    LLMRateLimitError,
    LLMUnavailableError,
)


class OpenAIEmbeddingProvider:
    """Real embeddings, dimension-compatible with `HashingEmbedder` on purpose:
    both emit 1536-dimensional vectors, so swapping the provider is a re-embed
    of existing chunks rather than a schema migration.
    """

    name = "openai"

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._model = model
        self.dimensions = 1536
        self._client = openai.AsyncOpenAI(api_key=api_key)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(model=self._model, input=texts)
        except openai.RateLimitError as exc:
            raise LLMRateLimitError("the model provider is rate limiting us") from exc
        except (openai.APITimeoutError, openai.APIConnectionError) as exc:
            raise LLMUnavailableError("could not reach the model provider") from exc
        except openai.APIStatusError as exc:
            # Mirrors `app/llm/openai_provider.py`'s mapping exactly, so both
            # adapters behave identically to callers: 401/403 is a bad or
            # missing key, any other 4xx is a request that will never
            # succeed, and only a 5xx is worth retrying.
            if 400 <= exc.status_code < 500:
                if exc.status_code in (401, 403):
                    raise LLMConfigurationError(
                        "the model provider rejected our credentials"
                    ) from exc
                raise LLMConfigurationError(
                    f"the model provider rejected our request ({exc.status_code})"
                ) from exc
            raise LLMUnavailableError(f"the model provider returned {exc.status_code}") from exc

        # The API does not guarantee the response arrives in request order;
        # sorting by `index` is what keeps each vector attached to the chunk
        # it was actually computed from.
        ordered = sorted(response.data, key=lambda item: item.index)
        return [item.embedding for item in ordered]

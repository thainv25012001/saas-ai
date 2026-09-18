from unittest.mock import AsyncMock, MagicMock

import httpx2
import openai
import pytest

from app.embeddings.openai_embedder import OpenAIEmbeddingProvider
from app.llm.errors import (
    LLMConfigurationError,
    LLMRateLimitError,
    LLMUnavailableError,
)

pytestmark = pytest.mark.anyio


def _embedding(index: int, vector: list[float]):
    return MagicMock(index=index, embedding=vector)


def _response(embeddings):
    return MagicMock(data=embeddings)


def _provider_with(response):
    provider = OpenAIEmbeddingProvider(api_key="test-key")
    provider._client = MagicMock()  # noqa: SLF001
    # `embeddings.create` is a real `async def` that returns its response
    # directly (unlike the chat completions stream) -- an `AsyncMock` with a
    # plain `return_value` reproduces that shape exactly: calling it returns
    # a coroutine, and awaiting that coroutine yields `response`. A
    # `MagicMock` here would make `await create(...)` raise `TypeError:
    # object MagicMock can't be used in 'await' expression`, which is the
    # exact bug PHASE-3's brief warns a prior fixture shipped with.
    provider._client.embeddings.create = AsyncMock(return_value=response)  # noqa: SLF001
    return provider


def _request_object() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/embeddings")


def _raising_provider(exc: Exception) -> OpenAIEmbeddingProvider:
    provider = OpenAIEmbeddingProvider(api_key="test-key")
    provider._client = MagicMock()  # noqa: SLF001
    provider._client.embeddings.create = AsyncMock(side_effect=exc)  # noqa: SLF001
    return provider


def _rate_limit_error() -> openai.RateLimitError:
    response = httpx2.Response(
        429, request=_request_object(), json={"error": {"type": "rate_limit_error"}}
    )
    return openai.RateLimitError("rate limited", response=response, body=None)


def _timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=_request_object())


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=_request_object())


def _status_error(status_code: int) -> openai.APIStatusError:
    response = httpx2.Response(
        status_code, request=_request_object(), json={"error": {"type": "some_error"}}
    )
    return openai.APIStatusError("failed", response=response, body=None)


def test_dimensions_are_1536():
    provider = OpenAIEmbeddingProvider(api_key="test-key")
    assert provider.dimensions == 1536


async def test_embed_returns_one_vector_per_text_in_order():
    response = _response([_embedding(0, [0.1, 0.2]), _embedding(1, [0.3, 0.4])])
    provider = _provider_with(response)
    vectors = await provider.embed(["alpha", "beta"])
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


async def test_embed_sends_the_batch_and_model_to_the_sdk():
    response = _response([_embedding(0, [0.1])])
    provider = _provider_with(response)
    await provider.embed(["alpha"])
    kwargs = provider._client.embeddings.create.call_args.kwargs  # noqa: SLF001
    assert kwargs["model"] == "text-embedding-3-small"
    assert kwargs["input"] == ["alpha"]


async def test_an_empty_batch_short_circuits_without_calling_the_sdk():
    provider = _provider_with(_response([]))
    assert await provider.embed([]) == []
    provider._client.embeddings.create.assert_not_called()  # noqa: SLF001


async def test_out_of_order_response_data_is_resorted_by_index():
    """The API does not guarantee response order. Trusting list order would
    attach every chunk's vector to the wrong chunk."""
    response = _response(
        [
            _embedding(2, [0.0, 0.0, 3.0]),
            _embedding(0, [1.0, 0.0, 0.0]),
            _embedding(1, [0.0, 2.0, 0.0]),
        ]
    )
    provider = _provider_with(response)
    vectors = await provider.embed(["alpha", "beta", "gamma"])
    assert vectors == [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 3.0]]


async def test_rate_limit_error_is_mapped_to_llm_rate_limit_error():
    provider = _raising_provider(_rate_limit_error())
    with pytest.raises(LLMRateLimitError):
        await provider.embed(["alpha"])


async def test_timeout_error_is_mapped_to_llm_unavailable_error():
    provider = _raising_provider(_timeout_error())
    with pytest.raises(LLMUnavailableError):
        await provider.embed(["alpha"])


async def test_connection_error_is_mapped_to_llm_unavailable_error():
    provider = _raising_provider(_connection_error())
    with pytest.raises(LLMUnavailableError):
        await provider.embed(["alpha"])


async def test_unauthenticated_status_error_is_mapped_to_llm_configuration_error():
    provider = _raising_provider(_status_error(401))
    with pytest.raises(LLMConfigurationError):
        await provider.embed(["alpha"])


async def test_forbidden_status_error_is_mapped_to_llm_configuration_error():
    provider = _raising_provider(_status_error(403))
    with pytest.raises(LLMConfigurationError):
        await provider.embed(["alpha"])


async def test_bad_request_status_error_is_mapped_to_llm_configuration_error():
    """A 400 must not be reported as `LLMUnavailableError`, which reads as
    transient and invites retrying a request that can never succeed."""
    provider = _raising_provider(_status_error(400))
    with pytest.raises(LLMConfigurationError):
        await provider.embed(["alpha"])


async def test_server_status_error_is_mapped_to_llm_unavailable_error():
    provider = _raising_provider(_status_error(503))
    with pytest.raises(LLMUnavailableError):
        await provider.embed(["alpha"])

from app.core.errors import AppError


class LLMError(AppError):
    """Base for every provider failure, normalized away from SDK-specific types.

    Callers never see an `anthropic.*` or `openai.*` exception: the SSE layer and the
    chat service would otherwise need to know which SDK produced a failure in order to
    render it.
    """

    code = "llm_error"
    status_code = 502


class LLMRateLimitError(LLMError):
    code = "rate_limited"
    status_code = 429


class LLMUnavailableError(LLMError):
    """Timeout, dropped connection, or a provider-side 5xx. Retrying may help."""

    code = "llm_unavailable"
    status_code = 503


class LLMConfigurationError(LLMError):
    """A missing or rejected API key, or a model this account cannot reach.

    Deliberately a 500: this is an operator problem, and telling the end user to
    "try again" would be a lie.
    """

    code = "llm_misconfigured"
    status_code = 500


class LLMEmptyResponseError(LLMError):
    code = "llm_empty_response"
    status_code = 502

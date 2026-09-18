from collections.abc import Iterable, Mapping
from typing import Any


class AppError(Exception):
    """Base for every error the application raises deliberately.

    Carries the HTTP status and a stable machine-readable code so the REST
    and GraphQL layers can render it identically without re-deciding.
    """

    code = "internal_error"
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(AppError):
    code = "not_found"
    status_code = 404


class ConflictError(AppError):
    code = "conflict"
    status_code = 409


class AuthenticationError(AppError):
    code = "unauthenticated"
    status_code = 401


class PermissionDeniedError(AppError):
    code = "forbidden"
    status_code = 403


class ValidationError(AppError):
    code = "invalid_input"
    status_code = 422


class RateLimitError(AppError):
    code = "rate_limited"
    status_code = 429


class PayloadTooLargeError(AppError):
    code = "payload_too_large"
    status_code = 413


def format_validation_errors(errors: Iterable[Mapping[str, Any]]) -> str:
    """Render pydantic's structured error list as one human-readable message.

    Shared by both surfaces so REST and GraphQL say the same thing about the
    same rejected payload: `app/main.py`'s RequestValidationError handler and
    `app/graphql/resolvers.py`'s `_build`.

    Deliberately reads only `loc` and `msg`. A pydantic error dict also
    carries `input` — the value the caller submitted, which for a
    registration is the plaintext password — and `url`, a link to
    errors.pydantic.dev that leaks the library's internals. Neither belongs
    in a response body, so neither is read here.
    """
    rendered: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "")).strip()
        rendered.append(f"{location}: {message}" if location and message else location or message)
    return "; ".join(part for part in rendered if part) or "invalid input"

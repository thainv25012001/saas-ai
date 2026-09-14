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

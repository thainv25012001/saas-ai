import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import auth, chat, health
from app.core.config import get_settings
from app.core.errors import AppError, format_validation_errors
from app.core.logging import configure_logging, get_logger, request_id_var
from app.core.security_headers import add_security_headers

# Starlette raises HTTPException for framework-level failures that never
# reach a route: unknown paths and wrong methods. Map each status onto the
# same stable `code` vocabulary the domain errors use, so a client only ever
# has to understand one set.
_HTTP_STATUS_CODES = {404: "not_found", 405: "invalid_input"}

logger = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="AI Sales Agent API", version="0.1.0")

    add_security_headers(app, settings.environment)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,  # required: the refresh token is a cookie
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        token = request_id_var.set(request_id)
        started = time.perf_counter()

        def log(status: int) -> None:
            """One structured access line per request.

            Method, path, status and duration only. Never the query string,
            headers or body: those carry passwords, bearer tokens and the
            refresh cookie.

            Always called from inside the try/finally below, before
            `request_id_var` is reset: `_add_request_id` binds `request_id`
            off that contextvar, and a line emitted after the reset would
            carry no id and so correlate with nothing - which is the whole
            point of having the id.
            """
            logger.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        try:
            response = await call_next(request)
        except BaseException:
            # An exception escaping the router's own handlers still gets a
            # line, so a failing request never silently disappears from the log.
            log(500)
            raise
        else:
            log(response.status_code)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        logger.warning("app_error", code=exc.code, message=exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's own handler would answer `{"detail": [...]}`, which is
        both off-envelope (the web client cannot read a `code` out of it, so
        every server-side validation failure renders as the generic
        "Something went wrong") and unsafe: each entry in that array carries
        an `input` key holding the rejected value verbatim, so a
        short-password registration ships the plaintext password straight
        back in the response body. `format_validation_errors` reads only
        `loc` and `msg`."""
        message = format_validation_errors(exc.errors())
        logger.warning("invalid_request", message=message)
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_input", "message": message}},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """Unknown routes and wrong methods are raised by Starlette itself and
        would otherwise answer `{"detail": "Not Found"}`. Same envelope as
        everything else, so a client has exactly one error shape to parse."""
        code = _HTTP_STATUS_CODES.get(exc.status_code, "internal_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": str(exc.detail)}},
            headers=getattr(exc, "headers", None),
        )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(chat.router)

    from strawberry.fastapi import GraphQLRouter

    from app.graphql.context import build_context
    from app.graphql.schema import schema

    app.include_router(
        GraphQLRouter(
            schema,
            context_getter=build_context,
            # strawberry-graphql>=0.250 renamed the boolean `graphiql` flag to
            # `graphql_ide`, which takes the IDE name or None.
            graphql_ide="graphiql" if settings.environment == "local" else None,
        ),
        prefix="/graphql",
    )

    return app


app = create_app()

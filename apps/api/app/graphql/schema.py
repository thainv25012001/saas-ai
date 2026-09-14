from collections.abc import Callable
from typing import Any

import strawberry
from graphql import GraphQLError, NoSchemaIntrospectionCustomRule
from strawberry.extensions import AddValidationRules, SchemaExtension

from app.core.config import get_settings
from app.core.errors import AppError
from app.graphql.resolvers import Mutation, Query


class AppErrorExtension(SchemaExtension):
    """Give GraphQL errors the same machine-readable codes the REST envelope
    uses, so the frontend has one error contract rather than two."""

    def on_operation(self) -> Any:
        yield
        result = self.execution_context.result
        if result is None or not result.errors:
            return
        for error in result.errors:
            original = error.original_error
            if isinstance(original, AppError):
                error.extensions = {**(error.extensions or {}), "code": original.code}
                error.message = original.message
            elif isinstance(error, GraphQLError) and error.original_error is None:
                error.extensions = {
                    **(error.extensions or {}),
                    "code": "invalid_input",
                }
            else:
                # Anything else - an unrecognised exception type, a bug in a
                # resolver, a database error - must never reach the client
                # with its own message. graphql-core's default behaviour is
                # `message = str(original_error)`, which for e.g. a raw
                # SQLAlchemy error would leak SQL text, constraint names, and
                # table names. Replace both message and code unconditionally.
                error.message = "internal server error"
                error.extensions = {**(error.extensions or {}), "code": "internal_error"}


def _build_extensions() -> list[type[SchemaExtension] | Callable[[], SchemaExtension]]:
    extensions: list[type[SchemaExtension] | Callable[[], SchemaExtension]] = [AppErrorExtension]
    if get_settings().environment != "local":
        # Introspection is not a secret here - the SDL is committed to
        # packages/shared/schema.graphql - but there is no reason to expose
        # it to unauthenticated callers outside local development. Kept as a
        # factory (not a constructed instance) so strawberry builds a fresh
        # extension per schema rather than warning about a shared instance.
        extensions.append(lambda: AddValidationRules([NoSchemaIntrospectionCustomRule]))
    return extensions


schema = strawberry.Schema(query=Query, mutation=Mutation, extensions=_build_extensions())

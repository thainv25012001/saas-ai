from typing import Any

import strawberry
from graphql import GraphQLError
from strawberry.extensions import SchemaExtension

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


schema = strawberry.Schema(query=Query, mutation=Mutation, extensions=[AppErrorExtension])

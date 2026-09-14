from fastapi import Request

from app.core.errors import AuthenticationError
from app.core.logging import request_id_var
from app.core.security import decode_token
from app.core.tenancy import TenantContext
from app.db.models import MembershipRole


def tenant_from_bearer(request: Request) -> TenantContext:
    """Build the tenant context from the Authorization header.

    Used by the REST routes and, from Task 9, by the GraphQL context.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("missing bearer token")

    payload = decode_token(token, expected_type="access")
    if payload.org is None or payload.role is None:
        raise AuthenticationError("token is missing organization context")

    try:
        role = MembershipRole(payload.role)
    except ValueError as exc:
        # A validly-signed token can still carry a role that no longer
        # exists (a role rename, a deploy rollback). A token-shaped input
        # must never produce a server error - treat it the same as any
        # other malformed token.
        raise AuthenticationError("token carries an unrecognized role") from exc

    return TenantContext(
        organization_id=payload.org,
        user_id=payload.sub,
        role=role,
        request_id=request_id_var.get(),
    )


async def get_current_tenant(request: Request) -> TenantContext:
    return tenant_from_bearer(request)

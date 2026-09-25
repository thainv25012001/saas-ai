from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select

from app.auth.dependencies import get_current_tenant
from app.auth.schemas import LoginRequest, MeResponse, RegisterRequest, TokenResponse
from app.auth.service import AuthService
from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.rate_limit import enforce_rate_limit
from app.core.request import client_ip
from app.core.security import decode_token, refresh_token_ttl_seconds
from app.core.tenancy import TenantContext, untenanted_session
from app.db.models import Membership, Organization, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        max_age=refresh_token_ttl_seconds(),
        httponly=True,  # unreachable from JavaScript, so XSS cannot steal it
        secure=get_settings().environment != "local",
        samesite="lax",
        # path="/" (not the narrower "/api/v1/auth"): a later task adds a
        # Next.js middleware route-guard on localhost:3000/dashboard that
        # checks for this cookie's presence. Cookies are shared across ports
        # on the same host but NOT across paths, so a cookie scoped to
        # /api/v1/auth would be invisible to that guard. Do not tighten this
        # path without updating the dashboard guard first.
        path="/",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, response: Response) -> TokenResponse:
    await enforce_rate_limit(f"register:{client_ip(request)}", limit=5, window_seconds=3600)
    async with untenanted_session() as session:
        service = AuthService(session)
        user, _organization, membership = await service.register(payload)
        access, refresh, _jti = service.issue_tokens(user, membership)

    _set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> TokenResponse:
    await enforce_rate_limit(f"login:{client_ip(request)}", limit=10, window_seconds=60)
    async with untenanted_session() as session:
        service = AuthService(session)
        user, membership = await service.authenticate(payload.email, payload.password)
        access, refresh, _jti = service.issue_tokens(user, membership)

    _set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.post("/refresh")
async def refresh_tokens(request: Request, response: Response) -> TokenResponse:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AuthenticationError("missing refresh token")

    async with untenanted_session() as session:
        access, refresh, _jti = await AuthService(session).rotate_refresh(token)

    _set_refresh_cookie(response, refresh)
    return TokenResponse(
        access_token=access,
        expires_in=get_settings().access_token_ttl_minutes * 60,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(token, expected_type="refresh")
        except AuthenticationError:
            payload = None  # already invalid; clearing the cookie is enough
        if payload is not None:
            async with untenanted_session() as session:
                await AuthService(session).revoke_refresh(payload.jti)
    response.delete_cookie(REFRESH_COOKIE, path="/")


@router.get("/me")
async def me(
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
) -> MeResponse:
    async with untenanted_session() as session:
        result = await session.execute(
            select(User, Organization, Membership)
            .join(Membership, Membership.user_id == User.id)
            .join(Organization, Organization.id == Membership.organization_id)
            .where(
                User.id == tenant.user_id,
                Membership.organization_id == tenant.organization_id,
                User.is_active.is_(True),
            )
        )
        row = result.first()
        if row is None:
            raise AuthenticationError("account no longer exists")
        user, organization, membership = row

        return MeResponse(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            organization_id=organization.id,
            organization_name=organization.name,
            role=membership.role.value,
        )

import uuid
from datetime import UTC, datetime

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import RegisterRequest
from app.core.errors import AuthenticationError, ConflictError
from app.core.ids import uuid7
from app.core.redis import get_redis
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    refresh_token_ttl_seconds,
    verify_password,
)
from app.db.models import Membership, MembershipRole, Organization, User

_DENYLIST_PREFIX = "refresh:revoked:"


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def register(self, request: RegisterRequest) -> tuple[User, Organization, Membership]:
        """Creates organization, user, and owner membership in one transaction.
        A partial result here would leave an account that cannot be used."""
        existing = await self.session.execute(select(User.id).where(User.email == request.email))
        if existing.scalar_one_or_none() is not None:
            raise ConflictError("an account with that email already exists")

        organization = Organization(
            id=uuid7(),
            name=request.organization_name,
            slug=await self._unique_slug(request.organization_name),
        )
        user = User(
            id=uuid7(),
            email=request.email,
            password_hash=hash_password(request.password),
            full_name=request.full_name,
        )
        self.session.add_all([organization, user])
        try:
            # Flushed before the membership row: organizations/users/memberships
            # have no ORM relationship() between them (deliberately - see
            # app/db/models), so the unit of work has no dependency edge
            # telling it membership must insert last. Flushing in two steps
            # inside this same transaction gets the FK order right without
            # relying on flush ordering it cannot infer, while keeping the
            # whole registration atomic (one transaction, from
            # untenanted_session).
            await self.session.flush()
        except IntegrityError as exc:
            # Two concurrent registrations for the same email: the unique
            # index is the authority, not the SELECT above.
            raise ConflictError("an account with that email already exists") from exc

        membership = Membership(
            id=uuid7(),
            organization_id=organization.id,
            user_id=user.id,
            role=MembershipRole.OWNER,
        )
        self.session.add(membership)
        await self.session.flush()
        return user, organization, membership

    async def _unique_slug(self, name: str) -> str:
        base = slugify(name)[:90] or "org"
        candidate = base
        for suffix in range(1, 100):
            taken = await self.session.execute(
                select(Organization.id).where(Organization.slug == candidate)
            )
            if taken.scalar_one_or_none() is None:
                return candidate
            candidate = f"{base}-{suffix}"
        return f"{base}-{uuid.uuid4().hex[:8]}"

    async def authenticate(self, email: str, password: str) -> tuple[User, Membership]:
        result = await self.session.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        # Hash even when the user is absent, so response time does not reveal
        # whether an email is registered.
        password_hash = user.password_hash if user else hash_password("dummy")
        if not verify_password(password, password_hash) or user is None:
            raise AuthenticationError("invalid email or password")
        if not user.is_active:
            raise AuthenticationError("invalid email or password")

        membership = await self._primary_membership(user.id)
        user.last_login_at = datetime.now(UTC)
        return user, membership

    async def _primary_membership(self, user_id: uuid.UUID) -> Membership:
        """Phase 1: a user belongs to exactly one organization, so the oldest
        membership is the active one. Org switching arrives with Phase 7."""
        result = await self.session.execute(
            select(Membership)
            .where(Membership.user_id == user_id)
            .order_by(Membership.created_at)
            .limit(1)
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise AuthenticationError("invalid email or password")
        return membership

    def issue_tokens(self, user: User, membership: Membership) -> tuple[str, str, str]:
        access = create_access_token(
            user_id=user.id,
            organization_id=membership.organization_id,
            role=membership.role.value,
        )
        refresh, jti = create_refresh_token(user_id=user.id)
        return access, refresh, jti

    async def rotate_refresh(self, refresh_token: str) -> tuple[str, str, str]:
        payload = decode_token(refresh_token, expected_type="refresh")
        if await get_redis().exists(f"{_DENYLIST_PREFIX}{payload.jti}"):
            raise AuthenticationError("invalid or expired token")

        result = await self.session.execute(select(User).where(User.id == payload.sub))
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise AuthenticationError("invalid or expired token")

        # Rotation: the presented token is burned as the new pair is issued,
        # so a stolen refresh token is usable at most once.
        await self.revoke_refresh(payload.jti)
        membership = await self._primary_membership(user.id)
        return self.issue_tokens(user, membership)

    async def revoke_refresh(self, jti: str) -> None:
        await get_redis().set(f"{_DENYLIST_PREFIX}{jti}", "1", ex=refresh_token_ttl_seconds())

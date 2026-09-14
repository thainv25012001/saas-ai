import pytest
from sqlalchemy import text

from app.core.ids import uuid7
from app.core.security import create_access_token

pytestmark = pytest.mark.anyio

REGISTRATION = {
    "email": "owner@example.com",
    "password": "correct-horse-battery",
    "full_name": "Ada Owner",
    "organization_name": "Ada Motors",
}


async def test_register_returns_an_access_token(client, clean_users):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 201
    body = response.json()
    assert body["access_token"]
    assert body["expires_in"] > 0


async def test_register_sets_an_httponly_refresh_cookie(client, clean_users):
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    cookie = response.headers.get("set-cookie", "")
    assert "refresh_token=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie.replace("SameSite=Lax", "SameSite=lax")


async def test_register_creates_org_user_and_owner_membership(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTRATION["email"], "password": REGISTRATION["password"]},
    )
    me = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    body = me.json()
    assert body["email"] == REGISTRATION["email"]
    assert body["organization_name"] == "Ada Motors"
    assert body["role"] == "owner"


async def test_register_rolls_back_organization_and_user_if_membership_fails(
    client, clean_users, monkeypatch, owner_connection
):
    """Registration inserts organization and user, then membership, in two
    flushes inside one transaction (see AuthService.register). This proves
    the whole thing is still atomic: a failure AFTER the org+user flush must
    still leave no orphan rows behind, not just a failure before any insert
    (which is all test_duplicate_email_is_a_conflict exercises)."""
    from app.db.models import Membership as MembershipModel

    def _boom(self: object, *args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated failure after organization/user flush")

    monkeypatch.setattr(MembershipModel, "__init__", _boom)

    with pytest.raises(RuntimeError):
        await client.post("/api/v1/auth/register", json=REGISTRATION)

    users = await owner_connection.execute(
        text("SELECT COUNT(*) FROM users WHERE email = :email"),
        {"email": REGISTRATION["email"]},
    )
    assert users.scalar_one() == 0

    orgs = await owner_connection.execute(
        text("SELECT COUNT(*) FROM organizations WHERE slug LIKE 'ada-motors%'")
    )
    assert orgs.scalar_one() == 0


async def test_duplicate_email_is_a_conflict(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post("/api/v1/auth/register", json=REGISTRATION)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_email_uniqueness_is_case_insensitive(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "email": "OWNER@example.com"}
    )
    assert response.status_code == 409


async def test_short_password_is_rejected(client, clean_users):
    response = await client.post(
        "/api/v1/auth/register", json={**REGISTRATION, "password": "short"}
    )
    assert response.status_code == 422


async def test_login_with_wrong_password_is_rejected(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": REGISTRATION["email"], "password": "wrong-password-entirely"},
    )
    assert response.status_code == 401


async def test_login_for_unknown_email_gives_the_same_error_as_wrong_password(client, clean_users):
    """Identical responses: a different one enumerates registered emails."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_me_requires_a_token(client):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_me_rejects_a_garbage_token(client):
    response = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer nonsense"})
    assert response.status_code == 401


async def test_me_rejects_a_token_carrying_an_unrecognized_role(client):
    """A validly-signed token can still carry a role that no longer exists
    (a role rename, a deploy rollback). It must be rejected as unauthenticated,
    not surfaced as a 500."""
    token = create_access_token(user_id=uuid7(), organization_id=uuid7(), role="superadmin")
    response = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_refresh_issues_a_new_access_token(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_refresh_without_a_cookie_is_rejected(client):
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


async def test_logout_revokes_the_refresh_token(client, clean_users):
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    # Save the cookie before logout clears it from the client's jar, so the
    # follow-up /refresh can re-present the SAME token explicitly. Otherwise
    # /refresh simply sees no cookie at all and 401s at the "missing refresh
    # token" guard, without ever consulting the denylist - which would let
    # this test pass even if revoke_refresh did nothing.
    saved_refresh = client.cookies.get("refresh_token")
    assert saved_refresh

    assert (await client.post("/api/v1/auth/logout")).status_code == 204

    # Re-present the saved token explicitly, on the client's own jar (a
    # per-request cookies= override is deprecated in httpx) - logout cleared
    # it from the jar, so this is the only way to prove the token itself, not
    # just "no cookie was sent", is what /refresh is rejecting.
    client.cookies.set("refresh_token", saved_refresh)
    response = await client.post("/api/v1/auth/refresh")
    assert response.status_code == 401


async def test_refresh_rotation_invalidates_the_presented_token(client, clean_users):
    """The security property of rotation: once a refresh token has been used,
    presenting that same token again must fail, even though it has not
    expired. Reuse of a stolen token is how theft gets detected."""
    await client.post("/api/v1/auth/register", json=REGISTRATION)
    first_refresh = client.cookies.get("refresh_token")
    assert first_refresh

    first_call = await client.post("/api/v1/auth/refresh")
    assert first_call.status_code == 200

    # The successful call above already rotated the client's jar to a new
    # cookie; set it back to the ORIGINAL, now-burned token to prove reuse
    # is rejected.
    client.cookies.set("refresh_token", first_refresh)
    reused = await client.post("/api/v1/auth/refresh")
    assert reused.status_code == 401


async def test_repeated_failed_logins_are_rate_limited(client, clean_users):
    """Ten attempts per minute per IP. Attempt eleven is refused."""
    for _ in range(10):
        await client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": "whatever-long-enough"},
        )
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "whatever-long-enough"},
    )
    assert response.status_code == 429

import pytest

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
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


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

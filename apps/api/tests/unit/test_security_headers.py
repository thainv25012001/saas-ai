"""The security headers every API response carries.

Nothing set these before: `strict-origin-when-cross-origin` showing up in a
browser's network panel was the browser's own default filling the silence, not
a header this app sent. These tests pin the real ones, and pin them on the
error paths too — a 404 renders HTML in some clients and a 422 is the response
an attacker sees most often, so headers that only appear on 200s protect the
one path nobody is attacking.
"""

import pytest
from httpx import AsyncClient

from app.core.security_headers import security_headers

pytestmark = pytest.mark.anyio


def test_hsts_is_omitted_locally() -> None:
    """Local dev is served over plain HTTP. Sending HSTS there pins a
    developer's browser to a scheme the dev server does not speak."""
    assert "Strict-Transport-Security" not in security_headers("local")


def test_hsts_is_sent_outside_local() -> None:
    assert security_headers("production")["Strict-Transport-Security"] == (
        "max-age=63072000; includeSubDomains"
    )


def test_hsts_is_not_preloaded() -> None:
    """The API answers on a hostname this project does not own. `preload`
    submits it to a list baked into browser binaries, which is slow and
    painful to undo, so it is deliberately absent."""
    assert "preload" not in security_headers("production")["Strict-Transport-Security"]


def test_the_api_refuses_to_be_framed() -> None:
    headers = security_headers("production")
    assert headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert headers["X-Frame-Options"] == "DENY"


def test_the_api_forbids_mime_sniffing() -> None:
    assert security_headers("production")["X-Content-Type-Options"] == "nosniff"


def test_the_api_sends_no_referrer() -> None:
    """A JSON API is not navigated from, so there is no referrer worth
    leaking — unlike the web app, which needs same-origin referrers."""
    assert security_headers("production")["Referrer-Policy"] == "no-referrer"


async def test_a_successful_response_carries_the_headers(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert response.headers["Referrer-Policy"] == "no-referrer"


async def test_an_unknown_path_carries_the_headers(client: AsyncClient) -> None:
    """404s are answered by Starlette's own handler, above the routers. The
    middleware has to wrap that too, or the most-probed paths on the service
    are the ones with no headers."""
    response = await client.get("/no-such-path")

    assert response.status_code == 404
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_a_validation_failure_carries_the_headers(client: AsyncClient) -> None:
    response = await client.post("/api/v1/auth/login", json={})

    assert response.status_code == 422
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_the_request_id_header_still_survives(client: AsyncClient) -> None:
    """Two middlewares now write to the same response. This fails the moment
    one starts replacing the other's headers instead of adding to them."""
    response = await client.get("/health", headers={"X-Request-ID": "req-under-test"})

    assert response.headers["X-Request-ID"] == "req-under-test"
    assert response.headers["X-Content-Type-Options"] == "nosniff"

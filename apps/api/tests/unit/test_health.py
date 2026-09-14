import pytest


@pytest.mark.anyio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.anyio
async def test_health_response_carries_request_id_header(client):
    response = await client.get("/health")
    assert response.headers["x-request-id"]


@pytest.mark.anyio
async def test_supplied_request_id_is_echoed(client):
    response = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["x-request-id"] == "abc-123"

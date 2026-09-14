import pytest

from app.core.errors import AppError, ConflictError, NotFoundError


def test_not_found_error_carries_status_and_code():
    error = NotFoundError("agent not found")
    assert error.status_code == 404
    assert error.code == "not_found"
    assert str(error) == "agent not found"


def test_conflict_error_carries_status_and_code():
    error = ConflictError("slug already taken")
    assert error.status_code == 409
    assert error.code == "conflict"


def test_subclasses_are_app_errors():
    assert isinstance(NotFoundError("x"), AppError)


@pytest.mark.anyio
async def test_app_error_is_rendered_as_json_envelope(client):
    """The app registers a handler that turns AppError into a stable envelope."""
    response = await client.get("/health/boom")
    assert response.status_code == 404
    assert response.json() == {"error": {"code": "not_found", "message": "deliberate test failure"}}

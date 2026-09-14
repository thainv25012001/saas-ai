import pytest

from app.core.errors import AppError, ConflictError, NotFoundError, format_validation_errors


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


def test_format_validation_errors_joins_location_and_message():
    errors = [
        {"loc": ("body", "password"), "msg": "String should have at least 12 characters"},
        {"loc": ("body", "email"), "msg": "value is not a valid email address"},
    ]
    assert format_validation_errors(errors) == (
        "body.password: String should have at least 12 characters; "
        "body.email: value is not a valid email address"
    )


def test_format_validation_errors_omits_the_submitted_input_and_pydantic_url():
    """The whole point of the shared formatter: pydantic's error dicts carry
    the rejected value under `input` (a plaintext password, for a
    registration) and a docs link under `url`. Neither may reach a client."""
    rendered = format_validation_errors(
        [
            {
                "type": "string_too_short",
                "loc": ("body", "password"),
                "msg": "String should have at least 12 characters",
                "input": "hunter2-secret",
                "url": "https://errors.pydantic.dev/2.11/v/string_too_short",
            }
        ]
    )
    assert "hunter2-secret" not in rendered
    assert "errors.pydantic.dev" not in rendered
    assert rendered == "body.password: String should have at least 12 characters"


def test_format_validation_errors_falls_back_when_the_list_is_empty():
    assert format_validation_errors([]) == "invalid input"

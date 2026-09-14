"""The access log the middleware emits for every request.

`request_context` already set and echoed `X-Request-ID` but logged nothing, so
the id correlated nothing: there was no line to correlate with. These tests
pin both halves — that a line is emitted at all, and that it carries the
request id — and that it carries nothing it should not.
"""

import pytest
import structlog
from structlog.testing import LogCapture

from app.core.logging import _add_request_id, configure_logging

pytestmark = pytest.mark.anyio


@pytest.fixture
def captured_logs(monkeypatch):
    """Capture through the real `_add_request_id` processor.

    `structlog.testing.capture_logs` replaces the whole processor chain, which
    would drop the very binding under test here, so the chain is rebuilt with
    just that processor and a capture sink.

    `app.main.logger` is rebound to a fresh proxy because structlog caches the
    bound logger (and its processor list) on a proxy's first use, and
    `app.main` is imported once for the whole session — without this, whether
    the capture sees anything would depend on whether some earlier test had
    already logged.
    """
    import app.main

    capture = LogCapture()
    structlog.configure(processors=[_add_request_id, capture])
    monkeypatch.setattr(app.main, "logger", structlog.get_logger("app.main.test"))
    try:
        yield capture.entries
    finally:
        configure_logging()


async def test_every_request_emits_one_access_log_line(client, captured_logs):
    response = await client.get("/health")
    assert response.status_code == 200

    lines = [entry for entry in captured_logs if entry["event"] == "request"]
    assert len(lines) == 1
    line = lines[0]
    assert line["method"] == "GET"
    assert line["path"] == "/health"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)


async def test_the_access_log_line_carries_the_request_id(client, captured_logs):
    """This is what makes the id worth plumbing: the header the client gets
    back names a line that actually exists in the log."""
    response = await client.get("/health", headers={"X-Request-ID": "req-under-test"})

    line = next(entry for entry in captured_logs if entry["event"] == "request")
    assert line["request_id"] == "req-under-test"
    assert response.headers["X-Request-ID"] == "req-under-test"


async def test_the_access_log_omits_the_query_string_and_headers(client, captured_logs):
    """Query strings and headers carry bearer tokens and the refresh cookie;
    a body can carry a plaintext password. The line is deliberately limited to
    method, path, status and duration."""
    await client.get(
        "/health?token=super-secret-value",
        headers={"Authorization": "Bearer secret-token"},
    )

    line = next(entry for entry in captured_logs if entry["event"] == "request")
    assert line["path"] == "/health"
    rendered = repr(line)
    assert "super-secret-value" not in rendered
    assert "secret-token" not in rendered


async def test_a_failing_request_is_still_logged(client, captured_logs):
    """/health/boom raises an AppError, which the exception handler turns into
    a 404 response; the request must not vanish from the log."""
    response = await client.get("/health/boom")
    assert response.status_code == 404

    line = next(entry for entry in captured_logs if entry["event"] == "request")
    assert line["path"] == "/health/boom"
    assert line["status"] == 404

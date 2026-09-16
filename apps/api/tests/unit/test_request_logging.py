"""The access log the middleware emits for every request.

`request_context` already set and echoed `X-Request-ID` but logged nothing, so
the id correlated nothing: there was no line to correlate with. These tests
pin both halves — that a line is emitted at all, and that it carries the
request id — and that it carries nothing it should not.

They also pin the one deliberate exception: a probe that passed is not logged.
`/openapi.json` stands in for "an ordinary request" precisely because the
probe paths no longer are one.
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


@pytest.fixture
def healthy_dependencies(monkeypatch):
    """`/health/ready` calls the real database and Redis. A unit run has
    neither, so without this the endpoint under test answers `degraded` and
    the suppression it is meant to demonstrate correctly does not apply."""
    from app.core import redis as redis_module
    from app.db import session as session_module

    async def ok() -> bool:
        return True

    monkeypatch.setattr(redis_module, "check_redis", ok)
    monkeypatch.setattr(session_module, "check_database", ok)


async def test_an_ordinary_request_emits_one_access_log_line(client, captured_logs):
    response = await client.get("/openapi.json")
    assert response.status_code == 200

    lines = [entry for entry in captured_logs if entry["event"] == "request"]
    assert len(lines) == 1
    line = lines[0]
    assert line["method"] == "GET"
    assert line["path"] == "/openapi.json"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], float)


async def test_the_access_log_line_carries_the_request_id(client, captured_logs):
    """This is what makes the id worth plumbing: the header the client gets
    back names a line that actually exists in the log."""
    response = await client.get("/openapi.json", headers={"X-Request-ID": "req-under-test"})

    line = next(entry for entry in captured_logs if entry["event"] == "request")
    assert line["request_id"] == "req-under-test"
    assert response.headers["X-Request-ID"] == "req-under-test"


async def test_the_access_log_omits_the_query_string_and_headers(client, captured_logs):
    """Query strings and headers carry bearer tokens and the refresh cookie;
    a body can carry a plaintext password. The line is deliberately limited to
    method, path, status and duration."""
    await client.get(
        "/openapi.json?token=super-secret-value",
        headers={"Authorization": "Bearer secret-token"},
    )

    line = next(entry for entry in captured_logs if entry["event"] == "request")
    assert line["path"] == "/openapi.json"
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


class TestProbesThatPassAreNotLogged:
    """docker-compose polls /health/ready every 3s and Render polls /health;
    at that rate a passing probe contributes ~28k identical lines a day and
    buries everything worth reading. The response is unchanged — only the
    access line is dropped, and only when the probe actually passed."""

    async def test_liveness_that_passes_is_not_logged(self, client, captured_logs):
        response = await client.get("/health")
        assert response.status_code == 200

        assert [entry for entry in captured_logs if entry["event"] == "request"] == []

    async def test_readiness_that_passes_is_not_logged(
        self, client, captured_logs, healthy_dependencies
    ):
        response = await client.get("/health/ready")
        assert response.json()["status"] == "ready"

        assert [entry for entry in captured_logs if entry["event"] == "request"] == []

    async def test_readiness_that_is_degraded_is_logged(
        self, client, captured_logs, healthy_dependencies, monkeypatch
    ):
        """A degraded readiness answers 200 — the body, not the status, is the
        verdict — so status alone cannot decide this. It is the one failure
        the suppression could swallow, which is why it is pinned here."""
        from app.core import redis as redis_module

        async def down() -> bool:
            return False

        # Everything healthy from the fixture, then exactly one thing knocked
        # over: the later `setattr` wins and is undone first.
        monkeypatch.setattr(redis_module, "check_redis", down)

        response = await client.get("/health/ready")
        assert response.json()["status"] == "degraded"

        line = next(entry for entry in captured_logs if entry["event"] == "request")
        assert line["path"] == "/health/ready"

    async def test_a_probe_that_fails_outright_is_logged(self, client, captured_logs, monkeypatch):
        """If the readiness handler itself raises, the middleware logs 500 and
        re-raises. Suppression keys off success, so this line survives."""
        from app.db import session as session_module

        async def boom() -> bool:
            raise RuntimeError("engine is gone")

        monkeypatch.setattr(session_module, "check_database", boom)

        with pytest.raises(RuntimeError):
            await client.get("/health/ready")

        line = next(entry for entry in captured_logs if entry["event"] == "request")
        assert line["path"] == "/health/ready"
        assert line["status"] == 500

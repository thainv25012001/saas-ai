"""Readiness must never raise, but it must never be silent either.

`/health/ready` answering `{"database": true, "redis": false}` is the whole
diagnosis the operator gets, and it is the same answer for a REST URL pasted
where a TCP one belongs, an unset variable falling back to localhost, and a
genuinely unreachable server. The reason belongs in the logs.
"""

import pytest
import structlog

from app.core.config import Settings


class TestRedisUrlScheme:
    """Upstash shows a REST endpoint (https://) and a TCP endpoint
    (rediss://) side by side. redis-py only speaks the latter; given the
    former it raises ValueError inside the readiness check, which is caught
    and reported as an unexplained `redis: false`. Rejecting it at startup
    turns a silent, permanent degradation into a boot error naming the fix.
    """

    def _settings(self, redis_url: str) -> Settings:
        return Settings(
            database_url="postgresql+asyncpg://u:p@h/db",
            migration_database_url="postgresql+asyncpg://u:p@h/db",
            jwt_secret="secret",
            redis_url=redis_url,
        )

    def test_rest_endpoint_is_rejected(self):
        with pytest.raises(ValueError, match="rediss://"):
            self._settings("https://apn1-example-12345.upstash.io")

    def test_http_endpoint_is_rejected(self):
        with pytest.raises(ValueError, match="rediss://"):
            self._settings("http://apn1-example-12345.upstash.io")

    def test_tls_tcp_endpoint_is_accepted(self):
        url = "rediss://default:pw@apn1-example-12345.upstash.io:6379"
        assert self._settings(url).redis_url == url

    def test_plain_tcp_endpoint_is_accepted(self):
        """Local development and the CI service container are not TLS."""
        assert self._settings("redis://localhost:6379/0").redis_url == "redis://localhost:6379/0"

    def test_unix_socket_is_accepted(self):
        assert self._settings("unix:///tmp/redis.sock").redis_url == "unix:///tmp/redis.sock"


class TestCheckLogging:
    @pytest.mark.anyio
    async def test_redis_failure_is_logged_with_its_reason(self, monkeypatch):
        from app.core import redis as redis_module

        def boom() -> object:
            raise ValueError("Redis URL must specify one of the following schemes")

        monkeypatch.setattr(redis_module, "get_redis", boom)
        with structlog.testing.capture_logs() as logs:
            assert await redis_module.check_redis() is False

        assert any(
            entry["event"] == "redis_unreachable" and "schemes" in entry["error"] for entry in logs
        ), logs

    @pytest.mark.anyio
    async def test_database_failure_is_logged_with_its_reason(self, monkeypatch):
        from app.db import session as session_module

        class Boom:
            def connect(self) -> object:
                raise OSError("connection refused")

        monkeypatch.setattr(session_module, "engine", Boom())
        with structlog.testing.capture_logs() as logs:
            assert await session_module.check_database() is False

        assert any(
            entry["event"] == "database_unreachable" and "refused" in entry["error"]
            for entry in logs
        ), logs

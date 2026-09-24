"""A production deploy whose `MCP_ALLOWED_HOSTS` is still loopback-only
answers every real `/mcp` request 421 (docs/PHASE-7.md §4). Nothing fails
at startup, so a warning is the only signal -- pinned here."""

import pytest
from structlog.testing import capture_logs

from app.core.config import Settings
from app.mcp.app import warn_if_mcp_hosts_loopback_only


def _settings(environment: str, hosts: list[str]) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h/d",
        migration_database_url="postgresql+asyncpg://u:p@h/d",
        jwt_secret="x",
        environment=environment,
        mcp_allowed_hosts=hosts,
    )


def _warnings(settings: Settings) -> list[dict[str, object]]:
    with capture_logs() as entries:
        warn_if_mcp_hosts_loopback_only(settings)
    return [e for e in entries if e["event"] == "mcp_allowed_hosts_loopback_only"]


@pytest.mark.parametrize(
    "hosts",
    [
        ["localhost:*", "127.0.0.1:*"],
        ["localhost"],
        ["127.0.0.1:8000", "[::1]:*", "::1"],
    ],
)
def test_production_with_only_loopback_hosts_warns(hosts: list[str]) -> None:
    [entry] = _warnings(_settings("production", hosts))
    assert entry["log_level"] == "warning"


def test_production_with_a_public_host_does_not_warn() -> None:
    assert _warnings(_settings("production", ["localhost:*", "api.example.com:*"])) == []


def test_local_with_only_loopback_hosts_does_not_warn() -> None:
    assert _warnings(_settings("local", ["localhost:*", "127.0.0.1:*"])) == []


def test_building_the_app_emits_the_warning_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired into startup, not just defined."""
    import app.main as main

    settings = _settings("production", ["localhost:*", "127.0.0.1:*"])
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    # `create_app` reconfigures structlog, which would replace capture_logs'
    # processors before the warning is emitted.
    monkeypatch.setattr(main, "configure_logging", lambda _level: None)
    with capture_logs() as entries:
        main.create_app()
    assert any(e["event"] == "mcp_allowed_hosts_loopback_only" for e in entries)

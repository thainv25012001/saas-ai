from app.core.config import _find_env_file, _normalize_database_url, get_settings


def test_find_env_file_walks_up_from_start_regardless_of_cwd(monkeypatch, tmp_path):
    """Settings.env_file must resolve from the module's own location, not the
    process cwd — alembic and the app are launched from different directories.

    Built entirely inside tmp_path rather than asserting on the real repo's
    .env: that file is gitignored, so a fresh checkout (CI, where config
    arrives as real environment variables) has none and the assertion would
    fail before testing any behaviour. The cwd is moved to a directory that
    contains no .env at all, so a cwd-relative implementation finds nothing
    and this test fails.
    """
    repo_root = tmp_path / "repo"
    module_path = repo_root / "apps" / "api" / "app" / "core" / "config.py"
    module_path.parent.mkdir(parents=True)
    expected = repo_root / ".env"
    expected.write_text("JWT_SECRET=from-the-walk\n", encoding="utf-8")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert _find_env_file(start=module_path) == expected


def test_find_env_file_prefers_the_nearest_env_walking_upwards(tmp_path):
    """The walk stops at the first .env above the start, so a nearer one
    shadows the repo-root one rather than both being ambiguous."""
    repo_root = tmp_path / "repo"
    api_root = repo_root / "apps" / "api"
    module_path = api_root / "app" / "core" / "config.py"
    module_path.parent.mkdir(parents=True)
    (repo_root / ".env").write_text("JWT_SECRET=far\n", encoding="utf-8")
    nearer = api_root / ".env"
    nearer.write_text("JWT_SECRET=near\n", encoding="utf-8")

    assert _find_env_file(start=module_path) == nearer


def test_find_env_file_returns_none_when_no_env_exists_above(tmp_path):
    """Inside the container image there is no .env above the module; the walk
    must return None rather than raising."""
    fake_module_path = tmp_path / "app" / "core" / "config.py"
    assert _find_env_file(start=fake_module_path) is None


def test_cors_origins_parses_comma_separated_real_env_var(monkeypatch):
    """pydantic-settings JSON-decodes complex-typed fields from real env vars
    before any field_validator runs. NoDecode suppresses that so the existing
    comma-split validator actually gets the raw string."""
    monkeypatch.setenv("CORS_ORIGINS", "http://a.test,http://b.test")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.cors_origins == ["http://a.test", "http://b.test"]
    finally:
        get_settings.cache_clear()


def test_cors_origins_single_value_without_comma_yields_one_element_list(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://solo.test")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.cors_origins == ["http://solo.test"]
    finally:
        get_settings.cache_clear()


def test_mcp_allowed_hosts_parses_comma_separated_real_env_var(monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "api.example.com, localhost:*")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        # `localhost:*` also admits a bare `localhost` (see the next tests).
        assert settings.mcp_allowed_hosts == ["api.example.com", "localhost:*", "localhost"]
    finally:
        get_settings.cache_clear()


def test_mcp_allowed_hosts_defaults_to_loopback_any_port_and_bare(monkeypatch):
    monkeypatch.delenv("MCP_ALLOWED_HOSTS", raising=False)
    from app.core.config import Settings

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.mcp_allowed_hosts == ["localhost:*", "localhost", "127.0.0.1:*", "127.0.0.1"]


def test_a_wildcard_port_entry_also_admits_the_bare_host(monkeypatch):
    """The SDK's `host:*` matches only a Host header that carries a port
    (`mcp/server/transport_security.py`), but an HTTPS client on 443 sends a
    bare `api.example.com`. Without this expansion the documented
    `MCP_ALLOWED_HOSTS=api.example.com:*` answers every real request 421."""
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "api.example.com:*")
    get_settings.cache_clear()
    try:
        assert get_settings().mcp_allowed_hosts == ["api.example.com:*", "api.example.com"]
    finally:
        get_settings.cache_clear()


def test_the_bare_host_expansion_dedupes_and_keeps_order(monkeypatch):
    monkeypatch.setenv(
        "MCP_ALLOWED_HOSTS", "api.example.com, api.example.com:*, other.test:8443, api.example.com"
    )
    get_settings.cache_clear()
    try:
        assert get_settings().mcp_allowed_hosts == [
            "api.example.com",
            "api.example.com:*",
            "other.test:8443",
        ]
    finally:
        get_settings.cache_clear()


def test_a_list_value_is_expanded_too():
    from app.core.config import Settings

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@h/d",
        migration_database_url="postgresql+asyncpg://u:p@h/d",
        jwt_secret="x",
        mcp_allowed_hosts=["api.example.com:*"],
    )
    assert settings.mcp_allowed_hosts == ["api.example.com:*", "api.example.com"]


class TestNormalizeDatabaseUrl:
    """Neon (like Render, Heroku and Supabase) hands out libpq-shaped URLs:
    a `postgresql://` scheme with no driver, and `sslmode`/`channel_binding`
    in the query string. Both are wrong for this app in ways that fail late
    and read as unrelated bugs — a driver-less scheme makes SQLAlchemy load
    psycopg2 (`ModuleNotFoundError: No module named 'psycopg2'`, at import
    time), and `sslmode` reaches `asyncpg.connect()` as an unexpected keyword
    argument (at first connect). Pasting the URL the provider gives you has
    to work."""

    def test_driverless_scheme_becomes_asyncpg(self):
        assert _normalize_database_url("postgresql://u:p@host/neondb") == (
            "postgresql+asyncpg://u:p@host/neondb"
        )

    def test_legacy_postgres_scheme_becomes_asyncpg(self):
        """`postgres://` is what several platforms still emit; SQLAlchemy
        dropped the alias and answers `Can't load plugin`."""
        assert _normalize_database_url("postgres://u:p@host/neondb") == (
            "postgresql+asyncpg://u:p@host/neondb"
        )

    def test_sslmode_is_translated_and_channel_binding_dropped(self):
        """asyncpg spells it `ssl`, and has no channel_binding parameter at
        all. TLS is preserved — the requirement, not the spelling, is what
        matters."""
        normalized = _normalize_database_url(
            "postgresql://u:p@host/neondb?sslmode=require&channel_binding=require"
        )
        assert normalized == "postgresql+asyncpg://u:p@host/neondb?ssl=require"

    def test_stronger_sslmode_values_survive(self):
        normalized = _normalize_database_url("postgresql://u:p@host/db?sslmode=verify-full")
        assert normalized == "postgresql+asyncpg://u:p@host/db?ssl=verify-full"

    def test_password_is_not_masked(self):
        """URL.render_as_string() masks the password by default; a masked
        password would turn this helper into an authentication failure."""
        assert "s3cr3t" in _normalize_database_url("postgresql://u:s3cr3t@host/db")

    def test_already_correct_url_is_unchanged(self):
        url = "postgresql+asyncpg://u:p@host/db?ssl=require"
        assert _normalize_database_url(url) == url

    def test_explicit_ssl_wins_over_sslmode(self):
        normalized = _normalize_database_url(
            "postgresql://u:p@host/db?ssl=verify-full&sslmode=require"
        )
        assert normalized == "postgresql+asyncpg://u:p@host/db?ssl=verify-full"

    def test_a_deliberately_chosen_driver_is_left_alone(self):
        """Only the driver-less forms are assumed to be a paste from a
        provider. Naming a driver is an explicit choice, so it is respected
        rather than silently overridden."""
        url = "postgresql+psycopg://u:p@host/db"
        assert _normalize_database_url(url) == url

    def test_settings_normalizes_both_database_urls(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db?sslmode=require")
        monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgres://o:p@host/db?sslmode=require")
        monkeypatch.setenv("JWT_SECRET", "test-secret")
        get_settings.cache_clear()
        try:
            settings = get_settings()
            assert settings.database_url == "postgresql+asyncpg://u:p@host/db?ssl=require"
            assert settings.migration_database_url == "postgresql+asyncpg://o:p@host/db?ssl=require"
        finally:
            get_settings.cache_clear()

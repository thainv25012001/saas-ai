from app.core.config import _find_env_file, get_settings


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

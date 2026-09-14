from pathlib import Path

from app.core.config import _find_env_file, get_settings


def test_find_env_file_locates_repo_root_env_regardless_of_cwd(monkeypatch, tmp_path):
    """Settings.env_file must resolve from this module's own location, not the
    process cwd — alembic and the app are launched from different directories."""
    expected = Path(__file__).resolve().parents[4] / ".env"
    assert expected.is_file(), "sanity check: repo-root .env is expected to exist"

    monkeypatch.chdir(tmp_path)
    assert _find_env_file() == expected


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

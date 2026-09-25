import pytest

from app.db.base import role_from_database_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+asyncpg://app_user:pw@localhost:5432/saas_ai", '"app_user"'),
        # Neon's own naming, and a mixed-case name that must survive exactly:
        # an unquoted identifier would be folded to lower case.
        (
            "postgresql+asyncpg://neondb_owner:pw@ep-x.neon.tech/neondb?ssl=require",
            '"neondb_owner"',
        ),
        ("postgresql+asyncpg://AppUser:pw@db/app", '"AppUser"'),
    ],
)
def test_the_role_is_the_url_username_quoted(url: str, expected: str) -> None:
    assert role_from_database_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://localhost/saas_ai",  # no user at all
        'postgresql+asyncpg://evil"; DROP TABLE x; --:pw@db/app',
        "postgresql+asyncpg://has space:pw@db/app",
    ],
)
def test_a_missing_or_unsafe_role_name_is_refused(url: str) -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        role_from_database_url(url)

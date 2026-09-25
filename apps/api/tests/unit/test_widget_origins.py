import pytest

from app.widget.origins import (
    MAX_ALLOWED_ORIGINS,
    InvalidOriginError,
    normalize_origin,
    normalize_origins,
)

# ---------------------------------------------------------------------------
# normalize_origin: accepted
# ---------------------------------------------------------------------------

_ACCEPTED = [
    ("https://Shop.Example.com/", False, "https://shop.example.com"),
    ("https://a.com:443", False, "https://a.com"),
    ("https://a.com:8443", False, "https://a.com:8443"),
    ("https://café.com", False, "https://xn--caf-dma.com"),
    ("http://localhost:3000", True, "http://localhost:3000"),
    ("http://127.0.0.1:8080", True, "http://127.0.0.1:8080"),
]


@pytest.mark.parametrize(("value", "allow_localhost_http", "expected"), _ACCEPTED)
def test_normalize_origin_accepts_and_canonicalizes(value, allow_localhost_http, expected):
    assert normalize_origin(value, allow_localhost_http=allow_localhost_http) == expected


# ---------------------------------------------------------------------------
# normalize_origin: rejected
# ---------------------------------------------------------------------------

_REJECTED = [
    "http://a.com",
    "https://a.com/path",
    "https://*.a.com",
    "https://u:p@a.com",
    "https://a.com?x=1",
    "https://a.com#f",
    "ftp://a.com",
    "a.com",
    "",
    # Final-review findings: hosts that are not DNS names, and ports that
    # used to escape as a bare ValueError (a 500 for the owner).
    "https://example.com;foo",
    "https://a.com https:",
    "https://a.com'self'",
    "https://a.com%20b",
    "https://-",
    "https://a-.com",
    "https://a..com",
    "https://[::1]",
    "https://[::1]:8443",
    "https://a.com:99999",
    "https://a.com:abc",
]


@pytest.mark.parametrize("value", _REJECTED)
def test_normalize_origin_rejects(value):
    with pytest.raises(InvalidOriginError) as excinfo:
        normalize_origin(value, allow_localhost_http=False)
    assert repr(value) in str(excinfo.value)


def test_normalize_origin_rejects_localhost_http_without_the_flag():
    with pytest.raises(InvalidOriginError):
        normalize_origin("http://localhost:3000", allow_localhost_http=False)


# ---------------------------------------------------------------------------
# normalize_origins
# ---------------------------------------------------------------------------


def test_normalize_origins_strips_blanks():
    assert normalize_origins(
        ["https://a.com", "  ", "", "https://b.com"], allow_localhost_http=False
    ) == ["https://a.com", "https://b.com"]


def test_normalize_origins_dedupes_preserving_first_seen_order():
    assert normalize_origins(
        ["https://b.com", "https://a.com", "https://b.com"], allow_localhost_http=False
    ) == ["https://b.com", "https://a.com"]


def test_normalize_origins_dedupes_after_canonicalization():
    """Two spellings of the same origin collapse to one entry -- the whole
    point of canonicalizing before comparing."""
    assert normalize_origins(
        ["https://Shop.Example.com/", "https://shop.example.com:443"],
        allow_localhost_http=False,
    ) == ["https://shop.example.com"]


def test_normalize_origins_raises_past_the_cap():
    values = [f"https://a{i}.com" for i in range(MAX_ALLOWED_ORIGINS + 1)]
    assert len(values) == 21
    with pytest.raises(InvalidOriginError):
        normalize_origins(values, allow_localhost_http=False)


def test_normalize_origins_checks_the_cap_before_normalizing_any_entry():
    """An oversized list is refused on its length alone -- the invalid last
    entry is never reached, so the error is about the cap."""
    values = [f"https://a{i}.com" for i in range(MAX_ALLOWED_ORIGINS)] + ["not-a-url"]
    with pytest.raises(InvalidOriginError) as excinfo:
        normalize_origins(values, allow_localhost_http=False)
    assert "at most" in str(excinfo.value)


def test_normalize_origins_cap_ignores_blank_entries():
    values = [f"https://a{i}.com" for i in range(MAX_ALLOWED_ORIGINS)] + ["", "  "]
    assert len(normalize_origins(values, allow_localhost_http=False)) == MAX_ALLOWED_ORIGINS


def test_normalize_origins_at_exactly_the_cap_is_fine():
    values = [f"https://a{i}.com" for i in range(MAX_ALLOWED_ORIGINS)]
    assert normalize_origins(values, allow_localhost_http=False) == values


def test_normalize_origins_propagates_the_first_invalid_value():
    with pytest.raises(InvalidOriginError) as excinfo:
        normalize_origins(["https://a.com", "not-a-url"], allow_localhost_http=False)
    assert repr("not-a-url") in str(excinfo.value)

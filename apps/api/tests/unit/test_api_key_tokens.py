import hashlib

from app.api_keys.tokens import (
    DISPLAY_PREFIX_LENGTH,
    TOKEN_PREFIX,
    display_prefix,
    generate_token,
    hash_token,
    looks_like_token,
)


def test_token_starts_with_the_prefix():
    assert generate_token().startswith(TOKEN_PREFIX)


def test_token_has_256_bits_of_entropy_worth_of_body_characters():
    """`secrets.token_urlsafe(32)` encodes 32 bytes (256 bits) as base64url
    with padding stripped: 43 characters. A shorter body would be silently
    weaker than the spec's "256 bits of entropy" claim (docs/PHASE-7.md
    §3)."""
    token = generate_token()
    body = token.removeprefix(TOKEN_PREFIX)
    assert len(body) == 43


def test_two_tokens_are_never_equal():
    assert generate_token() != generate_token()


def test_hash_token_is_sha256_of_the_utf8_bytes():
    token = "sa_mcp_" + "a" * 43
    assert hash_token(token) == hashlib.sha256(token.encode()).digest()


def test_hash_token_is_deterministic():
    token = generate_token()
    assert hash_token(token) == hash_token(token)


def test_hash_token_differs_for_different_tokens():
    assert hash_token(generate_token()) != hash_token(generate_token())


def test_display_prefix_is_the_token_prefix_plus_eight_chars():
    token = generate_token()
    prefix = display_prefix(token)
    assert len(prefix) == DISPLAY_PREFIX_LENGTH
    assert prefix == token[:DISPLAY_PREFIX_LENGTH]
    assert prefix.startswith(TOKEN_PREFIX)


def test_looks_like_token_accepts_a_real_token():
    assert looks_like_token(generate_token()) is True


def test_looks_like_token_rejects_missing_prefix():
    token = generate_token().removeprefix(TOKEN_PREFIX)
    assert looks_like_token(token) is False


def test_looks_like_token_rejects_wrong_length_body():
    assert looks_like_token(TOKEN_PREFIX + "short") is False
    assert looks_like_token(generate_token() + "x") is False


def test_looks_like_token_rejects_bad_characters():
    body = "a" * 43
    assert looks_like_token(TOKEN_PREFIX + body[:-1] + "!") is False
    assert looks_like_token(TOKEN_PREFIX + body[:-1] + " ") is False


def test_looks_like_token_rejects_empty_string():
    assert looks_like_token("") is False


def test_looks_like_token_rejects_none_shaped_near_miss():
    assert looks_like_token("sa_mcp_") is False


def test_looks_like_token_rejects_a_trailing_newline():
    """`re.match` with `$` accepts a single trailing newline -- `$` matches
    just before one -- so `fullmatch` is what pins "exactly the token"."""
    assert looks_like_token(generate_token() + "\n") is False

"""Token generation, hashing and shape-checking. No I/O -- the DB round trip
lives in `service.py`'s `resolve_api_key`.

Token format (docs/PHASE-7.md §3): `sa_mcp_` + 43 characters of
`secrets.token_urlsafe(32)` -- 256 bits of entropy, which is why the stored
`key_hash` is a plain SHA-256 rather than a slow password hash (argon2
exists to slow guessing of *low*-entropy secrets; this token has none of
that weakness, and a slow hash would add ~50ms to every MCP call for no
benefit).
"""

import hashlib
import re
import secrets

TOKEN_PREFIX = "sa_mcp_"

# "sa_mcp_" (7 chars) + 8 chars of the token body -- enough to tell two keys
# apart in the dashboard without showing anything close to the full secret.
DISPLAY_PREFIX_LENGTH = 15

# secrets.token_urlsafe(32) base64url-encodes 32 bytes (256 bits) and strips
# padding: ceil(32 * 8 / 6) = 43 characters, alphabet [A-Za-z0-9_-].
_TOKEN_BODY_BYTES = 32
_TOKEN_BODY_LENGTH = 43
_TOKEN_RE = re.compile(rf"^{re.escape(TOKEN_PREFIX)}[A-Za-z0-9_-]{{{_TOKEN_BODY_LENGTH}}}$")


def generate_token() -> str:
    """A fresh, unpredictable token. Never persisted -- only its hash is."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BODY_BYTES)


def hash_token(token: str) -> bytes:
    """The only form of the token that ever reaches the database."""
    return hashlib.sha256(token.encode()).digest()


def display_prefix(token: str) -> str:
    """What the dashboard shows in a key list: enough to identify a leaked
    key, nowhere near enough to reconstruct it."""
    return token[:DISPLAY_PREFIX_LENGTH]


def looks_like_token(value: str) -> bool:
    """Prefix, length and charset sanity -- checked before any DB call, so
    `resolve_api_key` never spends a query on something that plainly is not
    one of our tokens (and, per docs/PHASE-7.md §3, the lookup function
    would return nothing for it anyway)."""
    return _TOKEN_RE.match(value) is not None

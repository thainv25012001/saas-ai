"""Origin normalization for `widget_settings.allowed_origins`
(docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §3).

Pure, no I/O: this module never touches the database, Redis or the network.
Its whole job is to turn whatever a dashboard owner types into exactly what
later becomes a `frame-ancestors` source list -- a bare `scheme://host[:port]`,
nothing else. The web header builder (`apps/web/src/lib/frame-policy.ts`)
still re-validates every value it receives as defence in depth, but it should
never have anything to drop.

Rejecting a bad value here, rather than silently "fixing" it, is deliberate
(Review Focus #4): a wildcard, a path, or `http://` on a real host is a
mistake worth surfacing to the person who typed it, not a shape this module
quietly narrows before it ever reaches `frame-ancestors`.
"""

import re
from collections.abc import Sequence
from typing import NoReturn
from urllib.parse import urlsplit

MAX_ALLOWED_ORIGINS = 20

_DEFAULT_PORT = {"https": 443, "http": 80}
_LOCALHOST_HOSTS = {"localhost", "127.0.0.1"}
# A DNS name after IDNA encoding: dot-separated labels of [a-z0-9-], never
# starting or ending with a hyphen. Anything a CSP source list would parse
# differently (`;`, a space, a quote, `%`) fails this, and so does an IPv6
# literal. `localhost` and `127.0.0.1` both already match it.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class InvalidOriginError(ValueError):
    """Raised with a message naming the offending value and why."""


def normalize_origin(value: str, *, allow_localhost_http: bool) -> str:
    """Validate and canonicalize one origin.

    Accepts `https://host[:port]` always, and `http://localhost[:port]` /
    `http://127.0.0.1[:port]` only when `allow_localhost_http` is true (set
    by the caller from `ENVIRONMENT == "local"`) -- never in production,
    where the loader always talks to a real domain over TLS.

    Every other shape (a path, a query, a fragment, userinfo, a wildcard, a
    non-http(s) scheme, or a missing host) is rejected outright rather than
    stripped, per the module docstring above.
    """

    def _reject(reason: str) -> NoReturn:
        raise InvalidOriginError(f"{value!r} is not a valid origin: {reason}")

    if not value:
        _reject("must not be blank")

    parts = urlsplit(value)

    if parts.scheme not in ("https", "http"):
        _reject(f"scheme must be https, not {parts.scheme or '(missing)'}")
    if not parts.hostname:
        _reject("a host is required")
    if parts.username is not None or parts.password is not None:
        _reject("must not contain userinfo")
    if parts.path not in ("", "/"):
        _reject(f"must not contain a path ({parts.path!r})")
    if parts.query:
        _reject("must not contain a query string")
    if parts.fragment:
        _reject("must not contain a fragment")
    if "*" in value:
        _reject("must not contain a wildcard")

    if "[" in parts.netloc:
        _reject("IPv6 literals are not supported")

    # `_reject` is `NoReturn`, so mypy narrows `parts.hostname` to `str` via
    # the `not parts.hostname` check above.
    host = parts.hostname.lower()
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        _reject(f"host is not a valid domain name ({exc})")
    if not _HOST_RE.match(host):
        _reject("host is not a valid domain name")

    if parts.scheme == "http" and not (allow_localhost_http and host in _LOCALHOST_HOSTS):
        _reject("http is only allowed for localhost/127.0.0.1, and only in the local environment")

    try:
        port = parts.port
    except ValueError:
        _reject("port must be a number between 1 and 65535")
    if port == 0:
        _reject("port must be a number between 1 and 65535")
    if port is not None and port == _DEFAULT_PORT[parts.scheme]:
        port = None

    return f"{parts.scheme}://{host}" if port is None else f"{parts.scheme}://{host}:{port}"


def normalize_origins(values: Sequence[str], *, allow_localhost_http: bool) -> list[str]:
    """Strip blanks, cap at `MAX_ALLOWED_ORIGINS` non-blank entries, then
    normalize and validate each one and dedupe preserving first-seen order.

    A blank entry (empty or all whitespace) is silently dropped rather than
    rejected -- a dashboard textarea with a trailing empty line is not a
    mistake worth surfacing the way a malformed origin is.
    """
    entries = [stripped for raw in values if (stripped := raw.strip())]
    # Checked before any entry is parsed, so an oversized list costs nothing.
    if len(entries) > MAX_ALLOWED_ORIGINS:
        raise InvalidOriginError(
            f"at most {MAX_ALLOWED_ORIGINS} allowed origins are permitted, got {len(entries)}"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for stripped in entries:
        origin = normalize_origin(stripped, allow_localhost_http=allow_localhost_http)
        if origin not in seen:
            seen.add(origin)
            normalized.append(origin)
    return normalized

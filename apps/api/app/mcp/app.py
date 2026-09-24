"""The ASGI app `app/main.py` mounts at `/mcp` (docs/PHASE-7.md §4).

Stateless Streamable HTTP with JSON responses: the API may run as several
replicas and no tool streams. The session manager's `run()` must be entered
for the app's whole lifetime -- `app/main.py`'s lifespan does that.

`TransportSecuritySettings` is passed explicitly, always: the SDK only turns
DNS-rebinding (Host/Origin) protection on by default through a convenience
wrapper this embedding does not use, and leaves it off otherwise.
"""

from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.streamable_http_manager import (
    StreamableHTTPASGIApp,
    StreamableHTTPSessionManager,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import ASGIApp

from app.core.config import Settings
from app.core.logging import get_logger
from app.mcp.auth import ApiKeyTokenVerifier, RequireApiKey
from app.mcp.server import build_mcp_server

logger = get_logger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _host_without_port(entry: str) -> str:
    if entry.startswith("["):  # `[::1]` or `[::1]:8000`
        return entry[: entry.find("]") + 1] if "]" in entry else entry
    if entry.count(":") != 1:  # no port, or an unbracketed IPv6 address
        return entry
    return entry.rsplit(":", 1)[0]


def warn_if_mcp_hosts_loopback_only(settings: Settings) -> None:
    """A production deploy that never set `MCP_ALLOWED_HOSTS` boots fine and
    then answers every `/mcp` request 421 -- the default admits loopback
    only. Nothing else would say so, hence a startup warning."""
    if settings.environment != "production":
        return
    if all(_host_without_port(host) in _LOOPBACK_HOSTS for host in settings.mcp_allowed_hosts):
        logger.warning(
            "mcp_allowed_hosts_loopback_only",
            allowed_hosts=list(settings.mcp_allowed_hosts),
            hint="set MCP_ALLOWED_HOSTS to the API's public host, e.g. api.example.com:*",
        )


def build_mcp_asgi(settings: Settings) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """(the route's ASGI app, its session manager).

    Auth wraps only this app, so it applies to `/mcp` and nothing else: the
    rest of the API keeps its own JWT auth untouched.
    """
    warn_if_mcp_hosts_loopback_only(settings)
    session_manager = StreamableHTTPSessionManager(
        app=build_mcp_server(),
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.mcp_allowed_hosts),
            # Server-to-server callers send no Origin, which the SDK admits;
            # a browser that does send one must be the dashboard's own.
            allowed_origins=list(settings.cors_origins),
        ),
    )
    asgi_app: ASGIApp = AuthenticationMiddleware(
        RequireApiKey(StreamableHTTPASGIApp(session_manager)),
        backend=BearerAuthBackend(ApiKeyTokenVerifier()),
    )
    return asgi_app, session_manager

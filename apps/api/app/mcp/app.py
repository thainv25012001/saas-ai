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
from app.mcp.auth import ApiKeyTokenVerifier, RequireApiKey
from app.mcp.server import build_mcp_server


def build_mcp_asgi(settings: Settings) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """(the route's ASGI app, its session manager).

    Auth wraps only this app, so it applies to `/mcp` and nothing else: the
    rest of the API keeps its own JWT auth untouched.
    """
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

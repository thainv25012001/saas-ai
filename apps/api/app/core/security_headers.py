"""Response headers that harden every API reply.

Kept out of `request_context` in main.py on purpose: that middleware exists to
propagate the request id and emit the access line, and folding an unrelated
concern into it makes both harder to follow. This one is a pure mapping plus
the thinnest possible middleware over it, so the policy can be tested without
an ASGI app at all.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

# Two years, the value the HSTS preload list requires and a common floor for
# scanners. `preload` is deliberately absent - see `security_headers` below.
_HSTS = "max-age=63072000; includeSubDomains"


def security_headers(environment: str) -> dict[str, str]:
    """The headers to add to every response, given the deployment environment.

    `Strict-Transport-Security` is gated on the environment because local
    development is served over plain HTTP: a browser that accepts the header
    once will refuse the dev server's scheme afterwards, and the developer has
    to clear the pin by hand to recover.

    The value carries no `preload` token. Preloading submits the hostname to a
    list compiled into browser binaries, and the API answers on a hostname
    belonging to the hosting provider rather than to this project - a pin that
    would outlive any decision made here and take months to reverse.

    `X-Frame-Options` is strictly redundant with the CSP `frame-ancestors`
    directive on every browser still receiving updates. It stays for the ones
    that are not, at a cost of one line.
    """
    headers = {
        "Content-Security-Policy": "frame-ancestors 'none'",
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        # A JSON API is not navigated away from, so no referrer is useful to
        # anyone downstream. The web app sends a laxer policy because it needs
        # same-origin referrers for its own navigation.
        "Referrer-Policy": "no-referrer",
    }
    if environment != "local":
        headers["Strict-Transport-Security"] = _HSTS
    return headers


def add_security_headers(app: FastAPI, environment: str) -> None:
    """Register the middleware that applies `security_headers` to every reply.

    Registered as an `http` middleware rather than inside a route dependency
    so it also wraps the responses this app never routes: Starlette's own 404
    and 405, and the 422 the validation handler produces. Those are the
    replies an unfriendly client sees most, and they would otherwise be the
    only ones with no headers at all.
    """
    headers = security_headers(environment)

    @app.middleware("http")
    async def apply_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        # `update`, not assignment: the request-id middleware writes to the
        # same response object and neither may clobber the other.
        response.headers.update(headers)
        return response

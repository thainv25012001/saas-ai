"""Request helpers shared by more than one router."""

from fastapi import Request


def client_ip(request: Request) -> str:
    """The client address used as a rate-limit subject (auth routes, the
    public widget).

    It lives only inside rate-limit keys, which expire; it is never stored or
    logged (docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §5).
    """
    # request.client.host is the DIRECT peer's address. Phase 1 runs with no
    # proxy in front of this service, so that peer is the real client and
    # this is correct as-is. It stops being correct the moment a load
    # balancer or ingress sits in front: every client would then collapse
    # into the proxy's one IP (5 registrations/hour globally; one attacker
    # starving /login for everyone). Trusting X-Forwarded-For naively is NOT
    # the fix - it lets an attacker mint a fresh rate-limit key on every
    # request by forging the header. Before deploying behind a proxy,
    # configure Starlette/uvicorn's ProxyHeadersMiddleware with an explicit
    # trusted-hosts list (or run uvicorn with --proxy-headers
    # --forwarded-allow-ips=<the proxy's real address>) so only a header set
    # by that trusted hop is honored. Until then, this limiter is only
    # correct for direct connections.
    return request.client.host if request.client else "unknown"

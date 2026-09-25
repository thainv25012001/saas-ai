"""Request helpers shared by more than one router."""

from fastapi import Request


def client_ip(request: Request) -> str:
    """The client address used as a rate-limit subject (auth routes, the
    public widget).

    It lives only inside rate-limit keys, which expire; it is never stored or
    logged (docs/superpowers/specs/2026-09-25-embeddable-widget-design.md §5).
    """
    # request.client.host is the direct peer's address -- unless uvicorn runs
    # with --proxy-headers and the peer is in --forwarded-allow-ips, in which
    # case uvicorn has already replaced it with the X-Forwarded-For client.
    # `apps/api/Dockerfile` does exactly that, trusting loopback by default
    # and whatever FORWARDED_ALLOW_IPS names otherwise (render.yaml: '*', as
    # Render's proxy is the only way in). Trusting X-Forwarded-For from any
    # peer that can reach the container directly would let an attacker mint
    # a fresh rate-limit key per request by forging the header, so a deploy
    # without a proxy in front must leave it at the default. See
    # docs/DEPLOYMENT.md.
    return request.client.host if request.client else "unknown"

"""The deploy files carry the proxy-header settings the per-IP rate limits
depend on (docs/DEPLOYMENT.md): behind Render's proxy, without them every
visitor collapses into the proxy's one address."""

from pathlib import Path

_API_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _API_DIR.parents[1]


def test_dockerfile_trusts_forwarded_headers_only_from_a_configured_proxy():
    cmd = next(
        line
        for line in (_API_DIR / "Dockerfile").read_text().splitlines()
        if line.startswith("CMD")
    )
    assert "--proxy-headers" in cmd
    # Loopback by default: a plain `docker run` trusts no forwarded header.
    assert '--forwarded-allow-ips=\\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\\"' in cmd
    assert "--no-access-log" in cmd


def test_render_blueprint_trusts_its_own_proxy():
    blueprint = (_REPO_ROOT / "render.yaml").read_text()
    assert '- key: FORWARDED_ALLOW_IPS\n        value: "*"' in blueprint


def test_compose_leaves_forwarded_allow_ips_unset():
    assert "FORWARDED_ALLOW_IPS" not in (_REPO_ROOT / "docker-compose.yml").read_text()

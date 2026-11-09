"""Shared serving wiring: where to bind.

Used by the MCP servers *and* both agent servers. None of these services
authenticates its caller, so reachability is the authorization model: anything
that can open a socket to shell-exec gets command execution, and anything that
can reach the Operator drives the write agent with the Observer bypassed.

The default is therefore loopback, and binding wider is a deliberate act -
docker-compose sets ``MCP_BIND_HOST=0.0.0.0`` because there the container network
is the isolation boundary. Running outside Docker (the README's per-terminal
path) stays on localhost.
"""

from __future__ import annotations

import os

from mcp.server.transport_security import TransportSecuritySettings

from common.env import env_csv, env_int

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_ALLOWED_HOSTS = "127.0.0.1,localhost"


def bind_host() -> str:
    return os.environ.get("MCP_BIND_HOST", DEFAULT_BIND_HOST)


def port(*, default: int) -> int:
    return env_int("PORT", default=default)


def allowed_hosts(service: str | None = None) -> list[str]:
    """Hostnames permitted on the HTTP Host header (DNS-rebinding protection).

    ``MCP_ALLOWED_HOSTS`` extends the default loopback list for deployments;
    each service also accepts its own compose service name so peers can address
    it on the internal network.
    """
    extra = {service} if service else set()
    return sorted(env_csv("MCP_ALLOWED_HOSTS", default=DEFAULT_ALLOWED_HOSTS) | extra)


def transport_security(service: str) -> TransportSecuritySettings:
    """Host/Origin validation for a streamable-http MCP server.

    FastMCP passes ``transport_security=None`` straight to
    ``TransportSecurityMiddleware``, which reads None as
    ``enable_dns_rebinding_protection=False`` "for backwards compatibility" -
    the inverse of ``TransportSecuritySettings``' own default. Leaving it unset
    therefore turns the check off silently.

    That matters because these servers have no authentication. A browser on a
    machine running one of them can be pointed at an attacker domain that
    re-resolves to 127.0.0.1; the request is same-origin as far as CORS is
    concerned, so without a Host check the page reaches the tool surface -
    including shell-exec.

    ``MCP_ALLOWED_HOSTS`` extends the list for deployments (compose adds the
    service name, since peers address each other by it).
    """
    allowed = allowed_hosts(service)
    hosts = sorted({entry for host in allowed for entry in (host, f"{host}:*")})
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=sorted({f"http://{host}" for host in allowed} | {"null"}),
    )

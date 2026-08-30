"""Prometheus alerting READ-ONLY MCP server (Observer tool surface).

Queries Alertmanager for firing alerts and Prometheus for metric snapshots.
Whether those come from synthetic state or real endpoints is the backend's
business (``backends.alerts()``), not this module's.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from common.env import env_flag
from common.serving import bind_host, port, transport_security

from . import backends
from .backends.ports import DEFAULT_SEVERITY, SEVERITY_ORDER

mcp = FastMCP(
    "prometheus-alerts",
    host=bind_host(),
    port=port(default=8102),
    transport_security=transport_security("prometheus-alerts"),
)


@mcp.tool()
def active_alerts(min_severity: str = DEFAULT_SEVERITY) -> list[dict]:
    """Currently firing alerts at or above ``min_severity`` from Prometheus Alertmanager."""
    rank = SEVERITY_ORDER.get(min_severity, SEVERITY_ORDER[DEFAULT_SEVERITY])
    return backends.alerts().active_alerts(rank)


@mcp.tool()
def query_metric(query: str) -> dict:
    """Execute a PromQL instant query against Prometheus and return matching time-series."""
    return backends.alerts().query_metric(query)


if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    mcp.run(transport="streamable-http")

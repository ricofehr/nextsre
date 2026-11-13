"""Kubernetes READ-ONLY MCP server (Observer tool surface).

Only read verbs exist here, so the Observer's LLM cannot mutate the cluster even
by accident - there is literally no write tool to call.

Whether the data comes from synthetic state or a real cluster is the backend's
business (``backends.k8s_read()``), not this module's.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from common.env import env_flag
from common.serving import bind_host, port, transport_security
from common.telemetry import init_telemetry, traced_tool

from . import backends

mcp = MCPServer("k8s-readonly")

MAX_TAIL_LINES = 2000
MAX_EVENTS = 200


@mcp.tool()
@traced_tool
def list_pods(namespace: str = "payments") -> list[dict]:
    """List pods in a namespace with phase, readiness and restart counts."""
    return backends.k8s_read().list_pods(namespace)


@mcp.tool()
@traced_tool
def get_pod_logs(name: str, namespace: str = "payments", tail_lines: int = 50) -> str:
    """Fetch recent logs for a single pod."""
    return backends.k8s_read().pod_logs(name, namespace, _clamp(tail_lines, MAX_TAIL_LINES))


@mcp.tool()
@traced_tool
def recent_warning_events(namespace: str = "payments", limit: int = 20) -> list[dict]:
    """Recent Warning-type events (OOMKilled, FailedScheduling, ...)."""
    return backends.k8s_read().warning_events(namespace, _clamp(limit, MAX_EVENTS))


def _clamp(value: int, ceiling: int) -> int:
    """Keep LLM-supplied sizes within a sane range, preserving an explicit 0."""
    return max(0, min(value, ceiling))


if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    init_telemetry("k8s-ro")
    mcp.run(
        transport="streamable-http",
        host=bind_host(),
        port=port(default=8101),
        transport_security=transport_security("k8s-ro"),
    )

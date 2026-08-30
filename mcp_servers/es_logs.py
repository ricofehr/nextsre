"""Elasticsearch logs READ-ONLY MCP server (Observer tool surface).

Whether the documents come from synthetic state or a real index is the backend's
business (``backends.logs()``), not this module's. The bounds below are enforced
here because they are policy: ``size`` and ``minutes`` arrive from an LLM.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from common.env import env_flag
from common.serving import bind_host, port, transport_security

from . import backends

mcp = FastMCP(
    "es-logs",
    host=bind_host(),
    port=port(default=8103),
    transport_security=transport_security("es-logs"),
)

MAX_SIZE = 200
MAX_MINUTES = 1440


@mcp.tool()
def search_logs(
    query: str,
    service: str | None = None,
    minutes: int = 15,
    size: int = 50,
) -> list[dict]:
    """Search the last ``minutes`` of application logs.

    ``query`` is a free-text match over the message and level; ``service``
    optionally filters by app.
    """
    return backends.logs().search(
        query,
        service,
        _clamp(minutes, MAX_MINUTES),
        _clamp(size, MAX_SIZE),
    )


def _clamp(value: int, ceiling: int) -> int:
    """Keep LLM-supplied sizes within a sane range, preserving an explicit 0."""
    return max(0, min(value, ceiling))


if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    mcp.run(transport="streamable-http")

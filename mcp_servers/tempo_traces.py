"""Grafana Tempo traces READ-ONLY MCP server (Observer tool surface).

Exposes ``search_traces`` and ``get_trace`` so the Observer can inspect
distributed traces and correlate them with Loki log ``trace_id`` values.
Input bounds and ``trace_id`` hexadecimal validation are enforced here before
calling the backend because arguments arrive from an LLM reading untrusted data.
"""

from __future__ import annotations

import re

from mcp.server.mcpserver import MCPServer

from common.env import env_flag
from common.serving import bind_host, port, transport_security
from common.telemetry import init_telemetry, traced_tool

from . import backends

mcp = MCPServer("tempo-traces")

MAX_LIMIT = 100
MAX_MINUTES = 1440
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{16}([0-9a-fA-F]{16})?$")
_ALLOWED_STATUS = {"error", "ok", "unset"}


def _guard_trace_id(trace_id: str) -> str:
    cleaned = trace_id.strip()
    if not _TRACE_ID_RE.match(cleaned):
        raise ValueError(f"trace_id {trace_id!r} must be a 16- or 32-character hexadecimal string")
    return cleaned.lower()


def _guard_status(status: str | None) -> str | None:
    if status is None or not status.strip():
        return None
    cleaned = status.strip().lower()
    if cleaned not in _ALLOWED_STATUS:
        raise ValueError(f"status {status!r} must be one of {sorted(_ALLOWED_STATUS)}")
    return cleaned


@mcp.tool()
@traced_tool
def search_traces(
    service: str | None = None,
    min_duration_ms: int = 0,
    status: str | None = None,
    minutes: int = 15,
    limit: int = 20,
) -> list[dict]:
    """Search recent distributed traces in Grafana Tempo.

    Optionally filter by ``service`` name, ``min_duration_ms``, and span
    ``status`` (``"error"``, ``"ok"``, or ``"unset"``).
    """
    return backends.traces().search_traces(
        service,
        max(0, min_duration_ms),
        _guard_status(status),
        _clamp(minutes, MAX_MINUTES),
        _clamp(limit, MAX_LIMIT),
    )


@mcp.tool()
@traced_tool
def get_trace(trace_id: str) -> dict:
    """Fetch the spans of a single trace by its 16- or 32-hex-character ``trace_id``."""
    return backends.traces().get_trace(_guard_trace_id(trace_id))


def _clamp(value: int, ceiling: int) -> int:
    """Keep LLM-supplied sizes within a sane range, preserving an explicit 0."""
    return max(0, min(value, ceiling))


if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    init_telemetry("tempo-traces")
    mcp.run(
        transport="streamable-http",
        host=bind_host(),
        port=port(default=8104),
        transport_security=transport_security("tempo-traces"),
    )

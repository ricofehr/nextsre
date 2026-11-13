"""Ports: what the MCP tools need from a backend, independent of where it comes from.

These are ``typing.Protocol`` rather than ABCs so the demo and real adapters share
no base class - structural typing is enough to satisfy DIP here, and a 1.5k-line
repo does not need an inheritance hierarchy to prove it.
"""

from __future__ import annotations

from typing import Protocol

# One severity scale for the whole alerting path: the server ranks the caller's
# requested floor, the backends rank each alert against it. Three copies of this
# mapping could drift apart and silently filter out a severity nobody rejected.
SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}
DEFAULT_SEVERITY = "warning"


class K8sReadPort(Protocol):
    def list_pods(self, namespace: str) -> list[dict]: ...

    def pod_logs(self, name: str, namespace: str, tail_lines: int) -> str: ...

    def warning_events(self, namespace: str, limit: int) -> list[dict]: ...


class K8sWritePort(Protocol):
    def restart_deployment(self, name: str, namespace: str, stamp: str) -> dict: ...

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> dict: ...


class AlertsPort(Protocol):
    def active_alerts(self, min_severity_rank: int) -> list[dict]: ...

    def query_metric(self, query: str) -> dict: ...


class LogsPort(Protocol):
    def search(self, query: str, service: str | None, minutes: int, size: int) -> list[dict]: ...


class TracesPort(Protocol):
    def search_traces(
        self,
        service: str | None,
        min_duration_ms: int,
        status: str | None,
        minutes: int,
        limit: int,
    ) -> list[dict]: ...

    def get_trace(self, trace_id: str) -> dict: ...

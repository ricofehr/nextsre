"""Prometheus alerting READ-ONLY MCP server (Observer tool surface).

In a real deployment this queries the Prometheus Alertmanager API for firing
alerts and the Prometheus query API for metric snapshots. DEMO mode serves
synthetic data from shared state so it stays in sync with the rest of the incident.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import demo_state

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
mcp = FastMCP("prometheus-alerts", host="0.0.0.0", port=int(os.environ.get("PORT", "8102")))

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

ALERTMANAGER_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")


@mcp.tool()
def active_alerts(min_severity: str = "warning") -> list[dict]:
    """Currently firing alerts at or above ``min_severity`` from Prometheus Alertmanager."""
    floor = _SEVERITY_ORDER.get(min_severity, 1)
    if not DEMO:  # pragma: no cover
        import httpx

        resp = httpx.get(
            f"{ALERTMANAGER_URL}/api/v2/alerts",
            params={"active": "true", "silenced": "false", "inhibited": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        alerts = []
        for a in resp.json():
            severity = a.get("labels", {}).get("severity", "info")
            if _SEVERITY_ORDER.get(severity, 0) >= floor:
                ends_at = a.get("endsAt", "")
                state = "firing" if not ends_at or ends_at.startswith("0001") else "resolved"
                alerts.append({
                    "id": a.get("fingerprint", ""),
                    "severity": severity,
                    "service": a.get("labels", {}).get("job", a.get("labels", {}).get("service", "")),
                    "summary": a.get("annotations", {}).get("summary", a.get("annotations", {}).get("description", "")),
                    "state": state,
                })
        return alerts

    state = demo_state.load()
    return [
        a
        for a in state["alerts"]
        if a["state"] == "firing" and _SEVERITY_ORDER.get(a["severity"], 0) >= floor
    ]


@mcp.tool()
def query_metric(query: str, minutes: int = 5) -> dict:
    """Execute a PromQL instant query against Prometheus and return matching time-series."""
    if not DEMO:  # pragma: no cover
        import httpx

        resp = httpx.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    state = demo_state.load()
    results = []
    for dep in state["deployments"].values():
        value = dep.get("restarts", 0) if "restart" in query.lower() else (
            268435456 if not dep["healthy"] else 134217728
        )
        results.append({
            "metric": {"job": dep["name"], "namespace": dep["namespace"]},
            "value": [0, str(value)],
        })
    return {"status": "success", "data": {"resultType": "vector", "result": results}}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

"""Postgres alerting READ-ONLY MCP server (Observer tool surface).

In a real deployment this connects with a *read-only* database role to an
alerting/monitoring schema. DEMO mode serves synthetic alerts from shared
state so it stays in sync with the rest of the incident.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import demo_state

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
mcp = FastMCP("pg-alerts", host="0.0.0.0", port=int(os.environ.get("PORT", "8102")))

_SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


@mcp.tool()
def active_alerts(min_severity: str = "warning") -> list[dict]:
    """Currently firing alerts at or above ``min_severity``."""
    floor = _SEVERITY_ORDER.get(min_severity, 1)
    if not DEMO:  # pragma: no cover
        import psycopg

        dsn = os.environ["PG_DSN"]  # e.g. postgresql://readonly:***@db:5432/monitoring
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, severity, service, summary, state "
                "FROM alerts WHERE state = 'firing'"
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "severity": r[1], "service": r[2], "summary": r[3], "state": r[4]}
            for r in rows
            if _SEVERITY_ORDER.get(r[1], 0) >= floor
        ]

    state = demo_state.load()
    return [
        a
        for a in state["alerts"]
        if a["state"] == "firing" and _SEVERITY_ORDER.get(a["severity"], 0) >= floor
    ]


@mcp.tool()
def db_health() -> dict:
    """Connection saturation, replication lag and slow-query count."""
    if not DEMO:  # pragma: no cover
        import psycopg

        dsn = os.environ["PG_DSN"]
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity")
            used = cur.fetchone()[0]
            cur.execute("SHOW max_connections")
            mx = int(cur.fetchone()[0])
        return {"connections_used": used, "connections_max": mx}

    return demo_state.load()["db_health"]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

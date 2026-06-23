"""Elasticsearch logs READ-ONLY MCP server (Observer tool surface).

DEMO mode synthesises log documents from the shared incident state. Real mode
issues a bounded ``_search`` against an index pattern.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from . import demo_state

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
mcp = FastMCP("es-logs", host="0.0.0.0", port=int(os.environ.get("PORT", "8103")))


@mcp.tool()
def search_logs(
    query: str,
    service: str | None = None,
    minutes: int = 15,
    size: int = 50,
) -> list[dict]:
    """Search the last ``minutes`` of application logs.

    ``query`` is a free-text match; ``service`` optionally filters by app.
    """
    if not DEMO:  # pragma: no cover
        from elasticsearch import Elasticsearch

        es = Elasticsearch(os.environ.get("ES_URL", "http://elasticsearch:9200"))
        body = {
            "size": size,
            "sort": [{"@timestamp": "desc"}],
            "query": {
                "bool": {
                    "must": [{"query_string": {"query": query}}],
                    "filter": [{"range": {"@timestamp": {"gte": f"now-{minutes}m"}}}]
                    + ([{"term": {"service.keyword": service}}] if service else []),
                }
            },
        }
        hits = es.search(index="logs-*", body=body)["hits"]["hits"]
        return [h["_source"] for h in hits]

    now = datetime.now(timezone.utc).isoformat()
    state = demo_state.load()
    docs: list[dict] = []
    for dep in state["deployments"].values():
        if service and dep["name"] != service:
            continue
        if dep["healthy"]:
            docs.append(
                {"@timestamp": now, "service": dep["name"], "level": "INFO",
                 "message": "request handled ok"}
            )
        else:
            docs += [
                {"@timestamp": now, "service": dep["name"], "level": "ERROR",
                 "message": dep["last_error"]},
                {"@timestamp": now, "service": dep["name"], "level": "WARN",
                 "message": "container restart (exit code 137)"},
            ]
    q = query.lower()
    docs = [d for d in docs if q in d["message"].lower() or q in ("", "*", "error")]
    return docs[:size]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

"""Kubernetes READ-ONLY MCP server (Observer tool surface).

Only read verbs exist here, so the Observer's LLM cannot mutate the cluster
even by accident - there is literally no write tool to call.

DEMO mode (default) serves synthetic state from ``demo_state``. Set
``DEMO_MODE=0`` to use the real Kubernetes API (requires the ``kubernetes``
package and a kubeconfig / in-cluster service account).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import demo_state

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
mcp = FastMCP("k8s-readonly", host="0.0.0.0", port=int(os.environ.get("PORT", "8101")))


def _real_core():  # pragma: no cover - exercised only against a live cluster
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()


@mcp.tool()
def list_pods(namespace: str = "payments") -> list[dict]:
    """List pods in a namespace with phase, readiness and restart counts."""
    if not DEMO:  # pragma: no cover
        core, _ = _real_core()
        pods = core.list_namespaced_pod(namespace).items
        return [
            {
                "name": p.metadata.name,
                "namespace": namespace,
                "phase": p.status.phase,
                "restarts": sum(c.restart_count for c in (p.status.container_statuses or [])),
                "ready": all((c.ready for c in (p.status.container_statuses or [])), )
                if p.status.container_statuses
                else False,
            }
            for p in pods
        ]

    state = demo_state.load()
    out: list[dict] = []
    for dep in state["deployments"].values():
        if dep["namespace"] != namespace:
            continue
        for i in range(dep["replicas"]):
            out.append(
                {
                    "name": f"{dep['name']}-{i:02d}",
                    "namespace": namespace,
                    "phase": "Running" if dep["healthy"] else dep["reason"],
                    "restarts": dep["restarts"],
                    "ready": dep["healthy"],
                }
            )
    return out


@mcp.tool()
def get_pod_logs(name: str, namespace: str = "payments", tail_lines: int = 50) -> str:
    """Fetch recent logs for a single pod."""
    if not DEMO:  # pragma: no cover
        core, _ = _real_core()
        return core.read_namespaced_pod_log(name, namespace, tail_lines=tail_lines)

    state = demo_state.load()
    dep = next(
        (d for d in state["deployments"].values()
         if d["namespace"] == namespace and name.startswith(d["name"])),
        None,
    )
    if dep is None:
        return f"no logs: pod {name} not found in {namespace}"
    if dep["healthy"]:
        return "\n".join(f"INFO  request handled in {12 + i}ms" for i in range(5))
    return "\n".join(
        [
            "INFO  starting payments-api",
            "INFO  connected to payments-db",
            f"FATAL {dep['last_error']}",
            "INFO  container exited with code 137 (OOMKilled), restarting",
        ]
    )


@mcp.tool()
def recent_warning_events(namespace: str = "payments", limit: int = 20) -> list[dict]:
    """Recent Warning-type events (OOMKilled, FailedScheduling, ...)."""
    if not DEMO:  # pragma: no cover
        core, _ = _real_core()
        evs = [e for e in core.list_namespaced_event(namespace).items if e.type == "Warning"]
        return [
            {"reason": e.reason, "object": e.involved_object.name, "message": e.message}
            for e in evs[-limit:]
        ]

    state = demo_state.load()
    out: list[dict] = []
    for dep in state["deployments"].values():
        if dep["namespace"] == namespace and not dep["healthy"]:
            out.append(
                {
                    "reason": "BackOff",
                    "object": dep["name"],
                    "message": f"Back-off restarting failed container: {dep['last_error']}",
                }
            )
    return out[:limit]


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

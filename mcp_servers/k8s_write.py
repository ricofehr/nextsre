"""Kubernetes WRITE MCP server (Operator tool surface) + policy harness.

The harness is enforced *here*, inside the tools - the innermost,
non-bypassable line of defense. No prompt, caller, or LLM can talk its way
past these checks because they are plain code that runs before any mutation:

* namespace allowlist + protected-namespace denylist
* bounded parameters (replica cap)
* every action returns an audit record of exactly what was changed

In DEMO mode the mutation is applied to the shared state file (so the
Observer sees the incident actually resolve). Real mode calls the K8s API.
"""

from __future__ import annotations

import datetime
import os

from mcp.server.fastmcp import FastMCP

from . import demo_state

DEMO = os.environ.get("DEMO_MODE", "1") == "1"
mcp = FastMCP("k8s-write", host="0.0.0.0", port=int(os.environ.get("PORT", "8201")))

# ---- harness configuration -------------------------------------------------
ALLOWED_NAMESPACES = set(
    os.environ.get("ALLOWED_NAMESPACES", "web,payments,staging").split(",")
)
PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default"}
MAX_REPLICAS = int(os.environ.get("MAX_REPLICAS", "20"))


def _guard_ns(namespace: str) -> None:
    if namespace in PROTECTED_NAMESPACES:
        raise PermissionError(f"namespace '{namespace}' is protected; writes refused")
    if namespace not in ALLOWED_NAMESPACES:
        raise PermissionError(
            f"namespace '{namespace}' not in allowlist {sorted(ALLOWED_NAMESPACES)}"
        )


def _real_apps():  # pragma: no cover
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.AppsV1Api()


@mcp.tool()
def restart_deployment(name: str, namespace: str) -> dict:
    """Rolling-restart a deployment. Reversible, zero-downtime. Resolves crash loops."""
    _guard_ns(namespace)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if not DEMO:  # pragma: no cover
        apps = _real_apps()
        patch = {
            "spec": {"template": {"metadata": {"annotations": {
                "sre-agent/restartedAt": stamp}}}}
        }
        apps.patch_namespaced_deployment(name, namespace, patch)
        return {"action": "restart_deployment", "name": name,
                "namespace": namespace, "restartedAt": stamp}

    def _mutate(state: dict) -> None:
        key = f"{namespace}/{name}"
        dep = state["deployments"].get(key)
        if dep is None:
            raise ValueError(f"deployment {key} not found")
        dep.update(healthy=True, available=dep["replicas"], restarts=0,
                   reason="", last_error="")
        for alert in state["alerts"]:
            if alert["service"] == name:
                alert["state"] = "resolved"

    demo_state.update(_mutate)
    return {"action": "restart_deployment", "name": name, "namespace": namespace,
            "restartedAt": stamp, "result": "rollout triggered; pods recovered"}


@mcp.tool()
def scale_deployment(name: str, namespace: str, replicas: int) -> dict:
    """Scale a deployment. Hard-capped at MAX_REPLICAS by the harness."""
    _guard_ns(namespace)
    if not 0 <= replicas <= MAX_REPLICAS:
        raise ValueError(f"replicas must be between 0 and {MAX_REPLICAS}")

    if not DEMO:  # pragma: no cover
        apps = _real_apps()
        apps.patch_namespaced_deployment_scale(
            name, namespace, {"spec": {"replicas": replicas}})
        return {"action": "scale_deployment", "name": name,
                "namespace": namespace, "replicas": replicas}

    def _mutate(state: dict) -> None:
        key = f"{namespace}/{name}"
        dep = state["deployments"].get(key)
        if dep is None:
            raise ValueError(f"deployment {key} not found")
        dep["replicas"] = replicas
        if dep["healthy"]:
            dep["available"] = replicas

    demo_state.update(_mutate)
    return {"action": "scale_deployment", "name": name,
            "namespace": namespace, "replicas": replicas}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")

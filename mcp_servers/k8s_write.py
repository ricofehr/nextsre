"""Kubernetes WRITE MCP server (Operator tool surface) + policy harness.

The harness is enforced *here*, inside the tools - the innermost line of defense
for Kubernetes mutations. No prompt, caller, or LLM can talk its way past these
checks because they are plain code that runs before any mutation:

* protected-namespace denylist, evaluated before the allowlist
* namespace allowlist, which refuses everything when it parses empty
* RFC 1123 validation of every namespace and object name
* bounded parameters (replica cap)
* every action returns an audit record of exactly what was changed

Note that this harness only covers *this* server. ``shell_exec`` enforces its own,
and must keep ``kubectl`` read-only - otherwise a shell command would be a way
around everything above.
"""

from __future__ import annotations

import datetime
import re

from mcp.server.fastmcp import FastMCP

from common.env import env_csv, env_flag, env_int
from common.serving import bind_host, port

from . import backends

mcp = FastMCP("k8s-write", host=bind_host(), port=port(default=8201))

# ---- harness configuration -------------------------------------------------
PROTECTED_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease", "default"}
DEFAULT_ALLOWED_NAMESPACES = "web,payments,staging"
DEFAULT_MAX_REPLICAS = 20

# Kubernetes object names and namespaces are RFC 1123 labels. Validating them
# keeps anything shell- or path-shaped from reaching a backend in the first place.
_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_NAME_LENGTH = 63


def _allowed_namespaces() -> set[str]:
    """Read the allowlist at call time so the policy is not frozen at import."""
    return env_csv("ALLOWED_NAMESPACES", default=DEFAULT_ALLOWED_NAMESPACES)


def _max_replicas() -> int:
    return env_int("MAX_REPLICAS", default=DEFAULT_MAX_REPLICAS)


def _guard_name(value: str, kind: str) -> None:
    if not value or len(value) > _MAX_NAME_LENGTH or not _RFC1123.match(value):
        raise ValueError(f"{kind} {value!r} is not a valid RFC 1123 name")


def _guard_ns(namespace: str) -> None:
    _guard_name(namespace, "namespace")
    if namespace in PROTECTED_NAMESPACES:
        raise PermissionError(f"namespace '{namespace}' is protected; writes refused")
    allowed = _allowed_namespaces()
    if not allowed:
        raise PermissionError("ALLOWED_NAMESPACES is empty; all writes refused")
    if namespace not in allowed:
        raise PermissionError(f"namespace '{namespace}' not in allowlist {sorted(allowed)}")


def _guard_target(name: str, namespace: str) -> None:
    _guard_ns(namespace)
    _guard_name(name, "deployment")


@mcp.tool()
def restart_deployment(name: str, namespace: str) -> dict:
    """Rolling-restart a deployment. Reversible, zero-downtime. Resolves crash loops."""
    _guard_target(name, namespace)
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return backends.k8s_write().restart_deployment(name, namespace, stamp)


@mcp.tool()
def scale_deployment(name: str, namespace: str, replicas: int) -> dict:
    """Scale a deployment. Hard-capped at MAX_REPLICAS by the harness."""
    _guard_target(name, namespace)
    ceiling = _max_replicas()
    if not 0 <= replicas <= ceiling:
        raise ValueError(f"replicas must be between 0 and {ceiling}")
    return backends.k8s_write().scale_deployment(name, namespace, replicas)


if __name__ == "__main__":
    env_flag("DEMO_MODE", default=True)  # fail fast on a malformed value
    mcp.run(transport="streamable-http")

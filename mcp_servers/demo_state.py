"""File-backed 'cluster' state shared across the MCP server processes.

In DEMO mode every MCP server reads/writes the same JSON file (mounted on a
shared volume in docker-compose). That lets the *read* servers report a live
incident and the *write* server actually resolve it, so the Observer can
verify recovery after delegating to the Operator - a full closed loop with
no real Kubernetes/Prometheus/Elasticsearch required.

This is intentionally simple (atomic replace, last-write-wins). It is a demo
substrate, not a datastore.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Callable

DEFAULT_STATE_FILE = "/tmp/sre_demo/cluster_state.json"
_LOCK = threading.Lock()


def state_file() -> Path:
    """Resolved per call, not at import, so the path is never frozen before the
    environment is set (the servers are imported by tests and entrypoints alike)."""
    return Path(os.environ.get("DEMO_STATE_FILE", DEFAULT_STATE_FILE))


def _seed() -> dict:
    """Initial incident: payments-api is OOMKilling in a crash loop."""
    return {
        "deployments": {
            "payments/payments-api": {
                "namespace": "payments",
                "name": "payments-api",
                "replicas": 3,
                "available": 0,
                "healthy": False,
                "restarts": 47,
                "reason": "CrashLoopBackOff",
                "image": "registry.internal/payments-api:v2.4.1",
                "last_error": "OOMKilled: container exceeded memory limit 256Mi",
            },
            "web/web": {
                "namespace": "web",
                "name": "web",
                "replicas": 4,
                "available": 4,
                "healthy": True,
                "restarts": 0,
                "reason": "",
                "image": "registry.internal/web:v8.0.0",
                "last_error": "",
            },
        },
        "alerts": [
            {
                "id": "a-1001",
                "severity": "critical",
                "service": "payments-api",
                "summary": "payments-api pods in CrashLoopBackOff (OOMKilled)",
                "state": "firing",
            },
            {
                "id": "a-1002",
                "severity": "warning",
                "service": "payments-db",
                "summary": "connection pool saturation at 92%",
                "state": "firing",
            },
        ],
    }


def load() -> dict:
    with _LOCK:
        path = state_file()
        if not path.exists():
            _save_unlocked(_seed())
        return json.loads(path.read_text())


def _save_unlocked(state: dict) -> None:
    path = state_file()
    # 0700: the default path is a predictable directory under /tmp, and everything
    # in this file is fed to the Observer's LLM as evidence. A local user who can
    # write it controls what the agent believes about the incident.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)  # atomic


def update(mutator: Callable[[dict], None]) -> dict:
    """Read-modify-write the state under the in-process lock."""
    with _LOCK:
        path = state_file()
        state = json.loads(path.read_text()) if path.exists() else _seed()
        mutator(state)
        _save_unlocked(state)
        return state


def reset() -> dict:
    """Restore the seed incident (used by the demo runner)."""
    with _LOCK:
        state = _seed()
        _save_unlocked(state)
        return state

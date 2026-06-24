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

STATE_FILE = Path(os.environ.get("DEMO_STATE_FILE", "/tmp/sre_demo/cluster_state.json"))
_LOCK = threading.Lock()


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
        if not STATE_FILE.exists():
            _save_unlocked(_seed())
        return json.loads(STATE_FILE.read_text())


def _save_unlocked(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_FILE.parent))
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_FILE)  # atomic


def update(mutator: Callable[[dict], None]) -> dict:
    """Read-modify-write the state under the in-process lock."""
    with _LOCK:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else _seed()
        mutator(state)
        _save_unlocked(state)
        return state


def reset() -> dict:
    """Restore the seed incident (used by the demo runner)."""
    with _LOCK:
        _save_unlocked(_seed())
        return _seed()

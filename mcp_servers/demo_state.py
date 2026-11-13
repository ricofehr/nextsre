"""File-backed 'cluster' state shared across the MCP server processes.

In DEMO mode every MCP server reads/writes the same JSON file (mounted on a
shared volume in docker-compose). That lets the *read* servers report a live
incident and the *write* server actually resolve it, so the Observer can
verify recovery after delegating to the Operator - a full closed loop with
no real Kubernetes/Prometheus/Loki/Tempo required.

This is intentionally simple (atomic replace, last-write-wins). It is a demo
substrate, not a datastore.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

DEFAULT_STATE_FILE = "/tmp/sre_demo/cluster_state.json"
_LOCK = threading.Lock()


def state_file() -> Path:
    """Resolved per call, not at import, so the path is never frozen before the
    environment is set (the servers are imported by tests and entrypoints alike)."""
    return Path(os.environ.get("DEMO_STATE_FILE", DEFAULT_STATE_FILE))


@contextmanager
def _locked_state_path():
    """Acquire both the in-process thread lock and an advisory cross-process flock."""
    with _LOCK:
        path = state_file()
        # 0700: the default path is a predictable directory under /tmp, and everything
        # in this file is fed to the Observer's LLM as evidence. A local user who can
        # write it controls what the agent believes about the incident.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = path.with_suffix(f"{path.suffix}.lock")
        with lock_path.open("a+") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield path
            finally:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


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
    with _locked_state_path() as path:
        if not path.exists():
            _save_unlocked(path, _seed())
        return json.loads(path.read_text())


def _save_unlocked(path: Path, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, path)  # atomic
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(mutator: Callable[[dict], None]) -> dict:
    """Read-modify-write the state under both thread and file locks."""
    with _locked_state_path() as path:
        state = json.loads(path.read_text()) if path.exists() else _seed()
        mutator(state)
        _save_unlocked(path, state)
        return state


def reset() -> dict:
    """Restore the seed incident (used by the demo runner)."""
    with _locked_state_path() as path:
        state = _seed()
        _save_unlocked(path, state)
        return state

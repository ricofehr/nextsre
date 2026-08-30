"""Demo adapters: serve the synthetic incident from the shared state file.

All four adapters read (and the write one mutates) ``demo_state``, so the read
servers observe whatever the write server just did - that shared file is what
lets the Observer confirm its own remediation without a real cluster.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .. import demo_state
from .ports import SEVERITY_ORDER


class DemoK8sRead:
    def list_pods(self, namespace: str) -> list[dict]:
        pods: list[dict] = []
        for dep in demo_state.load()["deployments"].values():
            if dep["namespace"] != namespace:
                continue
            pods += [
                {
                    "name": f"{dep['name']}-{i:02d}",
                    "namespace": namespace,
                    # A crash-looping pod's phase really is Running; the
                    # CrashLoopBackOff signal is a container waiting reason. The
                    # demo reported it as the phase, which is a contract the real
                    # adapter cannot satisfy - so both now fill both fields.
                    "phase": "Running",
                    "reason": "" if dep["healthy"] else dep["reason"],
                    "restarts": dep["restarts"],
                    "ready": dep["healthy"],
                }
                for i in range(dep["replicas"])
            ]
        return pods

    def pod_logs(self, name: str, namespace: str, tail_lines: int) -> str:
        dep = next(
            (
                d
                for d in demo_state.load()["deployments"].values()
                if d["namespace"] == namespace and name.startswith(d["name"])
            ),
            None,
        )
        if dep is None:
            return f"no logs: pod {name} not found in {namespace}"
        if dep["healthy"]:
            lines = [f"INFO  request handled in {12 + i}ms" for i in range(5)]
        else:
            lines = [
                "INFO  starting payments-api",
                "INFO  connected to payments-db",
                f"FATAL {dep['last_error']}",
                "INFO  container exited with code 137 (OOMKilled), restarting",
            ]
        # lines[-0:] is the whole list, so 0 has to be handled before slicing -
        # the real K8s API treats tailLines=0 as no lines.
        if tail_lines <= 0:
            return ""
        return "\n".join(lines[-tail_lines:])

    def warning_events(self, namespace: str, limit: int) -> list[dict]:
        events = [
            {
                "reason": "BackOff",
                "object": dep["name"],
                "message": f"Back-off restarting failed container: {dep['last_error']}",
            }
            for dep in demo_state.load()["deployments"].values()
            if dep["namespace"] == namespace and not dep["healthy"]
        ]
        return events[:limit]


class DemoK8sWrite:
    def restart_deployment(self, name: str, namespace: str, stamp: str) -> dict:
        def mutate(state: dict) -> None:
            dep = _deployment(state, namespace, name)
            dep.update(
                healthy=True, available=dep["replicas"], restarts=0, reason="", last_error=""
            )
            for alert in state["alerts"]:
                if alert["service"] == name:
                    alert["state"] = "resolved"

        demo_state.update(mutate)
        return {
            "action": "restart_deployment",
            "name": name,
            "namespace": namespace,
            "restartedAt": stamp,
            "result": "rollout triggered; pods recovered",
        }

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> dict:
        def mutate(state: dict) -> None:
            dep = _deployment(state, namespace, name)
            dep["replicas"] = replicas
            if dep["healthy"]:
                dep["available"] = replicas

        demo_state.update(mutate)
        return {
            "action": "scale_deployment",
            "name": name,
            "namespace": namespace,
            "replicas": replicas,
        }


class DemoAlerts:
    def active_alerts(self, min_severity_rank: int) -> list[dict]:
        return [
            alert
            for alert in demo_state.load()["alerts"]
            if alert["state"] == "firing"
            and SEVERITY_ORDER.get(alert["severity"], 0) >= min_severity_rank
        ]

    def query_metric(self, query: str) -> dict:
        wants_restarts = "restart" in query.lower()
        results = [
            {
                "metric": {"job": dep["name"], "namespace": dep["namespace"]},
                "value": [0, str(_metric_value(dep, wants_restarts))],
            }
            for dep in demo_state.load()["deployments"].values()
        ]
        return {"status": "success", "data": {"resultType": "vector", "result": results}}


class DemoLogs:
    def search(self, query: str, service: str | None, minutes: int, size: int) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat()
        docs: list[dict] = []
        for dep in demo_state.load()["deployments"].values():
            if service and dep["name"] != service:
                continue
            if dep["healthy"]:
                docs.append(_doc(now, dep["name"], "INFO", "request handled ok"))
            else:
                docs += [
                    _doc(now, dep["name"], "ERROR", dep["last_error"]),
                    _doc(now, dep["name"], "WARN", "container restart (exit code 137)"),
                ]
        needle = query.strip().lower()
        matches_all = needle in ("", "*")
        docs = [d for d in docs if matches_all or needle in _haystack(d)]
        return docs[:size]


def _deployment(state: dict, namespace: str, name: str) -> dict:
    key = f"{namespace}/{name}"
    dep = state["deployments"].get(key)
    if dep is None:
        raise ValueError(f"deployment {key} not found")
    return dep


def _metric_value(dep: dict, wants_restarts: bool) -> int:
    if wants_restarts:
        return dep.get("restarts", 0)
    return 268435456 if not dep["healthy"] else 134217728


def _doc(timestamp: str, service: str, level: str, message: str) -> dict:
    return {"@timestamp": timestamp, "service": service, "level": level, "message": message}


def _haystack(doc: dict) -> str:
    """Free-text search covers the level as well as the message, so that the
    natural query "error" finds ERROR-level lines whatever their wording."""
    return f"{doc['level']} {doc['message']}".lower()

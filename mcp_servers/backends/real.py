"""Real adapters: Kubernetes, Prometheus/Alertmanager and Elasticsearch.

``kubernetes`` and ``elasticsearch`` ship in the optional ``real`` extra and are
usually absent, so every one of those imports is function-local: importing this
module must succeed on a demo-only install, which is what lets the selection in
``__init__`` stay a plain conditional instead of a guarded import.

Not exercised by the offline test suite - these paths need live infrastructure.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx

from .ports import SEVERITY_ORDER

_HTTP_TIMEOUT = 10
_MATCH_ALL = ("", "*")


def _kube_api():  # pragma: no cover - needs a live cluster
    """Load in-cluster credentials, falling back to a local kubeconfig."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api()


def _checked_url(name: str, default: str) -> str:
    """Return ``name`` from the environment, rejecting non-HTTP schemes."""
    url = os.environ.get(name, default)
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"{name}={url!r} must be an http(s) URL, got scheme {scheme!r}")
    return url.rstrip("/")


class RealK8sRead:  # pragma: no cover - needs a live cluster
    def list_pods(self, namespace: str) -> list[dict]:
        core, _ = _kube_api()
        pods = []
        for pod in core.list_namespaced_pod(namespace).items:
            statuses = pod.status.container_statuses or []
            pods.append(
                {
                    "name": pod.metadata.name,
                    "namespace": namespace,
                    "phase": pod.status.phase,
                    "restarts": sum(c.restart_count for c in statuses),
                    "ready": bool(statuses) and all(c.ready for c in statuses),
                }
            )
        return pods

    def pod_logs(self, name: str, namespace: str, tail_lines: int) -> str:
        core, _ = _kube_api()
        return core.read_namespaced_pod_log(name, namespace, tail_lines=tail_lines)

    def warning_events(self, namespace: str, limit: int) -> list[dict]:
        core, _ = _kube_api()
        events = [e for e in core.list_namespaced_event(namespace).items if e.type == "Warning"]
        return [
            {"reason": e.reason, "object": e.involved_object.name, "message": e.message}
            for e in events[-limit:]
        ]


class RealK8sWrite:  # pragma: no cover - needs a live cluster
    def restart_deployment(self, name: str, namespace: str, stamp: str) -> dict:
        _, apps = _kube_api()
        patch = {
            "spec": {"template": {"metadata": {"annotations": {"sre-agent/restartedAt": stamp}}}}
        }
        apps.patch_namespaced_deployment(name, namespace, patch)
        return {
            "action": "restart_deployment",
            "name": name,
            "namespace": namespace,
            "restartedAt": stamp,
            "result": "rollout triggered",
        }

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> dict:
        _, apps = _kube_api()
        apps.patch_namespaced_deployment_scale(name, namespace, {"spec": {"replicas": replicas}})
        return {
            "action": "scale_deployment",
            "name": name,
            "namespace": namespace,
            "replicas": replicas,
        }


class RealAlerts:  # pragma: no cover - needs a live Prometheus
    def active_alerts(self, min_severity_rank: int) -> list[dict]:
        url = _checked_url("ALERTMANAGER_URL", "http://localhost:9093")
        resp = httpx.get(
            f"{url}/api/v2/alerts",
            params={"active": "true", "silenced": "false", "inhibited": "false"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        alerts = []
        for alert in resp.json():
            labels = alert.get("labels", {})
            severity = labels.get("severity", "info")
            if SEVERITY_ORDER.get(severity, 0) < min_severity_rank:
                continue
            annotations = alert.get("annotations", {})
            ends_at = alert.get("endsAt", "")
            alerts.append(
                {
                    "id": alert.get("fingerprint", ""),
                    "severity": severity,
                    "service": labels.get("job", labels.get("service", "")),
                    "summary": annotations.get("summary", annotations.get("description", "")),
                    "state": "firing" if not ends_at or ends_at.startswith("0001") else "resolved",
                }
            )
        return alerts

    def query_metric(self, query: str) -> dict:
        url = _checked_url("PROMETHEUS_URL", "http://localhost:9090")
        resp = httpx.get(f"{url}/api/v1/query", params={"query": query}, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.json()


class RealLogs:  # pragma: no cover - needs a live Elasticsearch
    def search(self, query: str, service: str | None, minutes: int, size: int) -> list[dict]:
        from elasticsearch import Elasticsearch

        es = Elasticsearch(_checked_url("ES_URL", "http://elasticsearch:9200"))
        filters: list[dict] = [{"range": {"@timestamp": {"gte": f"now-{minutes}m"}}}]
        if service:
            filters.append({"term": {"service.keyword": service}})
        # A "match" on named fields, not "query_string": the query text comes from
        # an LLM reading attacker-influenceable logs, and query_string would let it
        # reach fields and operators this tool is not meant to expose.
        # "" and "*" mean match-all, matching DemoLogs - multi_match would treat
        # them as literal terms and return nothing.
        if query.strip() in _MATCH_ALL:
            must: dict = {"match_all": {}}
        else:
            must = {"multi_match": {"query": query, "fields": ["message", "level"]}}
        hits = es.search(
            index="logs-*",
            size=size,
            sort=[{"@timestamp": "desc"}],
            query={"bool": {"must": [must], "filter": filters}},
        )["hits"]["hits"]
        return [hit["_source"] for hit in hits]

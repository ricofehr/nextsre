"""Real adapters: Kubernetes, Prometheus/Alertmanager and Elasticsearch.

``kubernetes`` and ``elasticsearch`` ship in the optional ``real`` extra and are
usually absent, so every one of those imports is function-local: importing this
module must succeed on a demo-only install, which is what lets the selection in
``__init__`` stay a plain conditional instead of a guarded import.

Not exercised by the offline test suite - these paths need live infrastructure.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from urllib.parse import urlparse

import httpx

from .ports import SEVERITY_ORDER

_HTTP_TIMEOUT = 10
_MATCH_ALL = ("", "*")


@contextmanager
def _kube_api():  # pragma: no cover - needs a live cluster
    """Load in-cluster credentials (or local kubeconfig) with a managed ApiClient."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    with client.ApiClient() as api_client:
        yield client.CoreV1Api(api_client), client.AppsV1Api(api_client)


def _waiting_reason(statuses) -> str:  # pragma: no cover - needs a live cluster
    """The first container's waiting reason, e.g. CrashLoopBackOff.

    Kubernetes pod *phases* are only Pending/Running/Succeeded/Failed/Unknown -
    a crash-looping pod reports phase Running. The signal the Observer actually
    needs lives in the container status, so it is surfaced as its own field
    rather than folded into ``phase``.
    """
    for status in statuses:
        waiting = getattr(status.state, "waiting", None) if status.state else None
        if waiting and waiting.reason:
            return waiting.reason
    return ""


def _checked_url(name: str, default: str) -> str:
    """Return ``name`` from the environment, rejecting non-HTTP schemes."""
    url = os.environ.get(name, default)
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        raise ValueError(f"{name}={url!r} must be an http(s) URL, got scheme {scheme!r}")
    return url.rstrip("/")


class RealK8sRead:  # pragma: no cover - needs a live cluster
    def list_pods(self, namespace: str) -> list[dict]:
        with _kube_api() as (core, _):
            items = core.list_namespaced_pod(namespace).items
        pods = []
        for pod in items:
            statuses = pod.status.container_statuses or []
            pods.append(
                {
                    "name": pod.metadata.name,
                    "namespace": namespace,
                    "phase": pod.status.phase,
                    "reason": _waiting_reason(statuses),
                    "restarts": sum(c.restart_count for c in statuses),
                    "ready": bool(statuses) and all(c.ready for c in statuses),
                }
            )
        return pods

    def pod_logs(self, name: str, namespace: str, tail_lines: int) -> str:
        with _kube_api() as (core, _):
            return core.read_namespaced_pod_log(name, namespace, tail_lines=tail_lines)

    def warning_events(self, namespace: str, limit: int) -> list[dict]:
        with _kube_api() as (core, _):
            events = [e for e in core.list_namespaced_event(namespace).items if e.type == "Warning"]
        return [
            {"reason": e.reason, "object": e.involved_object.name, "message": e.message}
            for e in events[-limit:]
        ]


class RealK8sWrite:  # pragma: no cover - needs a live cluster
    def restart_deployment(self, name: str, namespace: str, stamp: str) -> dict:
        patch = {
            "spec": {"template": {"metadata": {"annotations": {"sre-agent/restartedAt": stamp}}}}
        }
        with _kube_api() as (_, apps):
            apps.patch_namespaced_deployment(name, namespace, patch)
        return {
            "action": "restart_deployment",
            "name": name,
            "namespace": namespace,
            "restartedAt": stamp,
            "result": "rollout triggered",
        }

    def scale_deployment(self, name: str, namespace: str, replicas: int) -> dict:
        with _kube_api() as (_, apps):
            apps.patch_namespaced_deployment_scale(
                name, namespace, {"spec": {"replicas": replicas}}
            )
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
            alerts.append(
                {
                    "id": alert.get("fingerprint", ""),
                    "severity": severity,
                    "service": labels.get("job", labels.get("service", "")),
                    "summary": annotations.get("summary", annotations.get("description", "")),
                    # The request already filters active/unsilenced/uninhibited, so
                    # everything here is firing. Deriving it from endsAt instead
                    # marked every alert "resolved": Alertmanager v2 always
                    # populates endsAt (startsAt + resolve_timeout for an active
                    # alert), never the Go zero time, so the check was always false.
                    "state": alert.get("status", {}).get("state", "firing"),
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
        with Elasticsearch(_checked_url("ES_URL", "http://elasticsearch:9200")) as es:
            hits = es.search(
                index="logs-*",
                size=size,
                sort=[{"@timestamp": "desc"}],
                query={"bool": {"must": [must], "filter": filters}},
            )["hits"]["hits"]
        return [hit["_source"] for hit in hits]

"""Real adapters: Kubernetes, Prometheus/Alertmanager, Grafana Loki, and Grafana Tempo.

``kubernetes`` ships in the optional ``real`` extra and is usually absent, so its
import is function-local: importing this module must succeed on a demo-only
install, which is what lets the selection in ``__init__`` stay a plain
conditional instead of a guarded import. Loki and Tempo are queried over HTTP
using ``httpx``.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from common.telemetry import inject_trace_headers

from .ports import SEVERITY_ORDER

_HTTP_TIMEOUT = 10
_MATCH_ALL = ("", "*")
_TRACE_ID_LINE_RE = re.compile(r'(?:trace_?[iI][dD])"?[:=]"?([0-9a-fA-F]{32})')
_LEVEL_TOKEN_RE = re.compile(r"\b(FATAL|CRITICAL|ERROR|WARN|WARNING|INFO|DEBUG)\b", re.IGNORECASE)


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


def _escape_string_literal(value: str) -> str:
    """Escape a double-quoted string literal in LogQL or TraceQL."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


def build_logql(query: str, service: str | None = None) -> str:
    """Construct a safe LogQL query without allowing selector or pipeline injection."""
    parts = ['{job=~".+"}']
    if service and service.strip():
        svc = service.strip()
        lit = _escape_string_literal(svc)
        re_lit = _escape_string_literal(re.escape(svc))
        parts.append(
            f'| service_name = "{lit}" or container_name = "{lit}" '
            f'or app = "{lit}" or pod_name =~ "^{re_lit}($|-.*)"'
        )
    cleaned_query = query.strip()
    if cleaned_query not in _MATCH_ALL:
        re_query = _escape_string_literal(re.escape(cleaned_query))
        parts.append(f'|~ "(?i){re_query}"')
    return " ".join(parts)


def build_traceql(
    service: str | None = None,
    min_duration_ms: int = 0,
    status: str | None = None,
) -> str:
    """Construct a safe TraceQL query without allowing attribute/operator injection."""
    clauses: list[str] = []
    if service and service.strip():
        lit = _escape_string_literal(service.strip())
        clauses.append(f'resource.service.name = "{lit}"')
    if status:
        if status not in ("error", "ok", "unset"):
            raise ValueError(f"invalid trace status {status!r}")
        clauses.append(f"status = {status}")
    if min_duration_ms > 0:
        clauses.append(f"duration >= {int(min_duration_ms)}ms")
    if not clauses:
        return "{}"
    return "{ " + " && ".join(clauses) + " }"


def _loki_auth() -> tuple[str, str] | None:
    user = os.environ.get("LOKI_USERNAME", "").strip()
    password = os.environ.get("LOKI_PASSWORD", "")
    if user and password:
        return (user, password)
    return None


def _parse_loki_entry(stream_labels: dict, entry: list) -> dict:
    ts_ns = int(entry[0])
    raw_line = str(entry[1])
    structured_meta = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}

    iso_ts = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc).isoformat()
    service = (
        stream_labels.get("service_name")
        or stream_labels.get("container_name")
        or stream_labels.get("app")
        or stream_labels.get("pod_name")
        or stream_labels.get("job")
        or ""
    )
    level = str(stream_labels.get("level") or structured_meta.get("level") or "")
    trace_id = str(
        structured_meta.get("trace_id")
        or structured_meta.get("traceID")
        or stream_labels.get("trace_id")
        or stream_labels.get("traceID")
        or ""
    )
    message = raw_line

    try:
        parsed = json.loads(raw_line)
    except (ValueError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        message = str(parsed.get("message") or parsed.get("log") or parsed.get("msg") or raw_line)
        if not level:
            level = str(parsed.get("level") or parsed.get("severity") or "")
        if not trace_id:
            trace_id = str(parsed.get("trace_id") or parsed.get("traceID") or "")

    if not trace_id:
        match = _TRACE_ID_LINE_RE.search(raw_line)
        if match:
            trace_id = match.group(1)

    if not level:
        lvl_match = _LEVEL_TOKEN_RE.search(message)
        level = lvl_match.group(1).upper() if lvl_match else "INFO"
    else:
        level = level.upper()

    return {
        "@timestamp": iso_ts,
        "service": service,
        "level": level,
        "message": message,
        "trace_id": trace_id,
        "_ts_ns": ts_ns,
    }


def _attr_map(attributes: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for attr in attributes:
        key = attr.get("key")
        val_obj = attr.get("value") or {}
        if not key or not isinstance(val_obj, dict):
            continue
        for vkey in ("stringValue", "intValue", "boolValue", "doubleValue"):
            if vkey in val_obj:
                out[str(key)] = str(val_obj[vkey])
                break
    return out


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
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=inject_trace_headers()) as http:
            resp = http.get(
                f"{url}/api/v2/alerts",
                params={"active": "true", "silenced": "false", "inhibited": "false"},
            )
            resp.raise_for_status()
            payload = resp.json()
        alerts = []
        for alert in payload:
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
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=inject_trace_headers()) as http:
            resp = http.get(f"{url}/api/v1/query", params={"query": query})
            resp.raise_for_status()
            return resp.json()


class RealLogs:
    def search(
        self,
        query: str,
        service: str | None = None,
        minutes: int = 15,
        size: int = 50,
    ) -> list[dict]:
        if size <= 0 or minutes <= 0:
            return []
        url = _checked_url("LOKI_URL", "http://loki-gateway.kube-logcollect.svc.cluster.local")
        now_ns = int(time.time() * 1_000_000_000)
        start_ns = now_ns - minutes * 60 * 1_000_000_000
        logql = build_logql(query, service)
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            auth=_loki_auth(),
            headers=inject_trace_headers(),
        ) as http:
            resp = http.get(
                f"{url}/loki/api/v1/query_range",
                params={
                    "query": logql,
                    "start": str(start_ns),
                    "end": str(now_ns),
                    "limit": str(size),
                    "direction": "backward",
                },
            )
            resp.raise_for_status()
            streams = resp.json().get("data", {}).get("result", [])

        docs: list[dict] = []
        for stream in streams:
            labels = stream.get("stream") or {}
            for entry in stream.get("values") or []:
                if isinstance(entry, list) and len(entry) >= 2:
                    docs.append(_parse_loki_entry(labels, entry))
        docs.sort(key=lambda d: d.pop("_ts_ns", 0), reverse=True)
        for doc in docs:
            doc.pop("_ts_ns", None)
        return docs[:size]


class RealTraces:
    def search_traces(
        self,
        service: str | None = None,
        min_duration_ms: int = 0,
        status: str | None = None,
        minutes: int = 15,
        limit: int = 20,
    ) -> list[dict]:
        if limit <= 0 or minutes <= 0:
            return []
        url = _checked_url("TEMPO_URL", "http://tempo.kube-tracing.svc.cluster.local:3200")
        now_s = int(time.time())
        start_s = now_s - minutes * 60
        traceql = build_traceql(service, min_duration_ms, status)
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=inject_trace_headers()) as http:
            resp = http.get(
                f"{url}/api/search",
                params={
                    "q": traceql,
                    "start": str(start_s),
                    "end": str(now_s),
                    "limit": str(limit),
                },
            )
            resp.raise_for_status()
            raw_traces = resp.json().get("traces") or []

        results: list[dict] = []
        for item in raw_traces[:limit]:
            item_status = status or "ok"
            for span_set in item.get("spanSets") or []:
                for span in span_set.get("spans") or []:
                    code = str((span.get("status") or {}).get("code", "")).lower()
                    if code in ("error", "status_code_error", "2"):
                        item_status = "error"
            results.append(
                {
                    "trace_id": item.get("traceID", ""),
                    "root_service": item.get("rootServiceName", ""),
                    "root_span": item.get("rootTraceName", ""),
                    "duration_ms": item.get("durationMs", 0),
                    "status": item_status,
                    "start_time_unix_nano": item.get("startTimeUnixNano", ""),
                }
            )
        return results

    def get_trace(self, trace_id: str) -> dict:
        url = _checked_url("TEMPO_URL", "http://tempo.kube-tracing.svc.cluster.local:3200")
        with httpx.Client(timeout=_HTTP_TIMEOUT, headers=inject_trace_headers()) as http:
            resp = http.get(f"{url}/api/traces/{trace_id}")
            resp.raise_for_status()
            payload = resp.json()

        spans: list[dict] = []
        root_service = ""
        overall_status = "ok"
        for batch in payload.get("batches") or []:
            res_attrs = _attr_map((batch.get("resource") or {}).get("attributes") or [])
            service_name = res_attrs.get("service.name", "")
            if not root_service and service_name:
                root_service = service_name
            scope_spans = batch.get("scopeSpans") or batch.get("instrumentationLibrarySpans") or []
            for scope in scope_spans:
                for span in scope.get("spans") or []:
                    status_obj = span.get("status") or {}
                    code = str(status_obj.get("code", "")).upper()
                    span_status = "error" if code in ("STATUS_CODE_ERROR", "2", "ERROR") else "ok"
                    if span_status == "error":
                        overall_status = "error"
                    start_ns = int(span.get("startTimeUnixNano") or 0)
                    end_ns = int(span.get("endTimeUnixNano") or start_ns)
                    duration_ms = max(0, (end_ns - start_ns) // 1_000_000)
                    spans.append(
                        {
                            "span_id": span.get("spanId", ""),
                            "service": service_name,
                            "operation": span.get("name", ""),
                            "duration_ms": duration_ms,
                            "status": span_status,
                            "error": status_obj.get("message", ""),
                            "attributes": {
                                **res_attrs,
                                **_attr_map(span.get("attributes") or []),
                            },
                        }
                    )
        return {
            "trace_id": trace_id,
            "root_service": root_service,
            "status": overall_status,
            "spans": spans,
        }

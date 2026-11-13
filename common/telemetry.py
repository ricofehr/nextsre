"""Opt-in OpenTelemetry tracing and W3C context propagation.

When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset or blank, ``init_telemetry`` is a
strict no-op: no ``BatchSpanProcessor`` or exporter is registered and no network
socket is opened, keeping offline unit tests and demo mode self-contained.

When ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (for example to the cluster's
``http://otel-collector.kube-tracing.svc.cluster.local:4317``), a
``TracerProvider`` with a ``BatchSpanProcessor`` is configured for either
``grpc`` (port 4317 default) or ``http/protobuf`` (port 4318 / ``/v1/traces``).

Spans record operational metadata only (service name, route, tool name, status)
and never attach prompt text, model completions, or secret values.
"""

from __future__ import annotations

import functools
import inspect
import os
from collections.abc import Callable, Mapping
from typing import Any, TypeVar
from urllib.parse import urlparse

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode

from common.env import env_flag

F = TypeVar("F", bound=Callable[..., Any])

_SUPPORTED_PROTOCOLS = ("grpc", "http/protobuf")
_ACTIVE_PROVIDER: TracerProvider | None = None
_NOOP_PROVIDER = trace.NoOpTracerProvider()


class _DelegatingTracerProvider(trace.TracerProvider):
    """Routes global ``opentelemetry.trace.get_tracer`` calls to ``_ACTIVE_PROVIDER``."""

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Any = None,
    ) -> trace.Tracer:
        target: trace.TracerProvider = _ACTIVE_PROVIDER or _NOOP_PROVIDER
        return target.get_tracer(
            instrumenting_module_name,
            instrumenting_library_version,
            schema_url,
            attributes,
        )


trace.set_tracer_provider(_DelegatingTracerProvider())


def _resolve_protocol(endpoint: str) -> str:
    raw = os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip().lower()
    if raw:
        if raw not in _SUPPORTED_PROTOCOLS:
            raise ValueError(
                f"OTEL_EXPORTER_OTLP_PROTOCOL={raw!r} must be one of {_SUPPORTED_PROTOCOLS}"
            )
        return raw
    parsed = urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
    if parsed.port == 4318 or parsed.path.rstrip("/").endswith("/v1/traces"):
        return "http/protobuf"
    return "grpc"


def _build_exporter(endpoint: str, protocol: str) -> SpanExporter:
    if "://" in endpoint:
        scheme = urlparse(endpoint).scheme
        if scheme not in ("http", "https"):
            raise ValueError(
                f"OTEL_EXPORTER_OTLP_ENDPOINT={endpoint!r} must use http(s) or host:port"
            )
    if protocol == "http/protobuf":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        http_url = endpoint if "://" in endpoint else f"http://{endpoint}"
        if not http_url.rstrip("/").endswith("/v1/traces"):
            http_url = f"{http_url.rstrip('/')}/v1/traces"
        return OTLPSpanExporter(endpoint=http_url)

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    if "OTEL_EXPORTER_OTLP_INSECURE" in os.environ:
        insecure = env_flag("OTEL_EXPORTER_OTLP_INSECURE", default=True)
    else:
        insecure = not endpoint.startswith("https://")
    return OTLPSpanExporter(endpoint=endpoint, insecure=insecure)


def build_tracer_provider(default_service_name: str) -> TracerProvider | None:
    """Construct a configured ``TracerProvider`` if ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return None
    service_name = os.environ.get("OTEL_SERVICE_NAME", "").strip() or default_service_name
    protocol = _resolve_protocol(endpoint)
    exporter = _build_exporter(endpoint, protocol)
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def init_telemetry(default_service_name: str) -> bool:
    """Initialize OpenTelemetry tracing when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set."""
    global _ACTIVE_PROVIDER
    provider = build_tracer_provider(default_service_name)
    if provider is None:
        return False
    if _ACTIVE_PROVIDER is not None:
        _ACTIVE_PROVIDER.shutdown()
    _ACTIVE_PROVIDER = provider
    return True


def set_tracer_provider_for_testing(provider: TracerProvider | None) -> None:
    """Override the active ``TracerProvider`` (used by unit tests with in-memory exporters)."""
    global _ACTIVE_PROVIDER
    _ACTIVE_PROVIDER = provider


def current_tracer_provider() -> TracerProvider | None:
    """Return the active ``TracerProvider`` configured by ``init_telemetry``."""
    return _ACTIVE_PROVIDER


def get_tracer(name: str = "nextsre") -> trace.Tracer:
    return trace.get_tracer(name)


def inject_trace_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Inject W3C ``traceparent`` / ``tracestate`` into ``headers`` and return it."""
    carrier: dict[str, str] = dict(headers) if headers else {}
    propagate.inject(carrier)
    return carrier


def extract_trace_context(headers: Mapping[str, str]) -> Any:
    """Extract W3C trace context from an HTTP header mapping."""
    normalized = {k.lower(): v for k, v in headers.items()}
    return propagate.extract(normalized)


def traced_tool(fn: F) -> F:
    """Wrap an MCP tool callable in an OpenTelemetry span without recording arguments."""
    span_name = f"mcp.tool.{fn.__name__}"
    attrs = {"mcp.tool.name": fn.__name__}

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with get_tracer(fn.__module__).start_as_current_span(
                span_name, attributes=attrs
            ) as span:
                try:
                    result = await fn(*args, **kwargs)
                except Exception:
                    span.set_attribute("mcp.tool.status", "error")
                    span.set_status(Status(StatusCode.ERROR))
                    raise
                span.set_attribute("mcp.tool.status", "ok")
                span.set_status(Status(StatusCode.OK))
                return result

        return _async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        with get_tracer(fn.__module__).start_as_current_span(span_name, attributes=attrs) as span:
            try:
                result = fn(*args, **kwargs)
            except Exception:
                span.set_attribute("mcp.tool.status", "error")
                span.set_status(Status(StatusCode.ERROR))
                raise
            span.set_attribute("mcp.tool.status", "ok")
            span.set_status(Status(StatusCode.OK))
            return result

    return _sync_wrapper  # type: ignore[return-value]
